"""Always-available sandbox runtime for this project's own dev/CI
environment, which has no gVisor/Firecracker installed (see
gvisor_runtime.py's docstring for the production choice and why it isn't
exercised here). Real isolation from the actual Linux primitives available
without special hardware/kernel support:

- A fresh network namespace per worker process (`unshare --net`) --
  genuinely blocks every outbound connection attempt at the OS level,
  independent of anything Python-level (proven in
  tests/test_engine_sandbox.py by a raw, unrestricted script run through
  spawn_worker_process() directly).
- `resource.setrlimit` for CPU/memory (worker_main.py, inside the child).
- A path-confined `open()` and import allowlist (restricted_exec.py).
- A wall-clock timeout (SIGALRM inside the worker, backstopped by a
  parent-side ceiling here in case a worker is wedged for a reason its own
  alarm can't catch).

This class is deliberately one-off: every execute() call spawns a fresh
worker process and tears it down afterward. Reusing an already-isolated,
already-booted worker across many calls (the warm pool, Build Spec §9) is
warm_pool.py's job, built on the same spawn_worker_process()/send_request()
primitives this module exposes.
"""

import json
import select
import subprocess
import sys
from pathlib import Path

from src.engine.sandbox.types import SandboxError, SandboxLimits, SandboxResult

# --user --map-root-user: a network namespace needs CAP_SYS_ADMIN unless it
# is created inside a fresh user namespace, and Docker's default capability
# set withholds CAP_SYS_ADMIN -- so a bare `unshare --net` 500'd every
# strategy create in the Docker stack. The user namespace grants nothing
# outside the worker; the backend's seccomp profile
# (deploy/seccomp/backend.json) allows unshare() for user+net namespaces only.
UNSHARE_NET_ARGS: tuple[str, ...] = ("unshare", "--user", "--map-root-user", "--net", "--")


class SandboxWorkerCrashedError(SandboxError):
    pass


class SandboxWorkerHungError(SandboxError):
    pass


def spawn_worker_process() -> subprocess.Popen:
    """The one place `unshare --net` + the worker subprocess is spawned --
    both the cold one-off runtime below and the warm pool build on this
    exact primitive, so there's only one code path whose network isolation
    needs to be trusted (and tested).
    """
    cmd = [*UNSHARE_NET_ARGS, sys.executable, "-u", "-m", "src.engine.sandbox.worker_main"]
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def send_request(proc: subprocess.Popen, request: dict, *, timeout_seconds: float) -> dict:
    """One newline-delimited JSON request/response round-trip against an
    already-spawned worker (see worker_main.py for the protocol). Raises
    rather than blocking forever if the worker doesn't answer in time or
    has died -- a caller that gets either exception should not reuse this
    worker (a warm pool replaces it; the one-off runtime just tears it
    down either way).
    """
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    proc.stdin.write(json.dumps(request) + "\n")
    proc.stdin.flush()

    ready, _, _ = select.select([proc.stdout], [], [], timeout_seconds)
    if not ready:
        raise SandboxWorkerHungError(
            f"sandbox worker did not respond within {timeout_seconds}s "
            "(its own internal timeout should have fired first -- this is a backstop)"
        )
    line = proc.stdout.readline()
    if not line:
        stderr = proc.stderr.read()
        raise SandboxWorkerCrashedError(f"sandbox worker exited unexpectedly: {stderr.strip()}")
    return json.loads(line)


def terminate_worker(proc: subprocess.Popen) -> None:
    try:
        if proc.stdin:
            proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


class RestrictedProcessSandboxRuntime:
    def execute(
        self,
        code: str,
        *,
        params: dict,
        scratch_dir: Path,
        data_dir: Path,
        limits: SandboxLimits | None = None,
    ) -> SandboxResult:
        limits = limits or SandboxLimits()
        proc = spawn_worker_process()
        try:
            request = {
                "code": code,
                "params": params,
                "scratch_dir": str(scratch_dir),
                "data_dir": str(data_dir),
                "limits": {
                    "cpu_seconds": limits.cpu_seconds,
                    "memory_bytes": limits.memory_bytes,
                    "timeout_seconds": limits.timeout_seconds,
                },
            }
            # Generous vs. the worker's own SIGALRM timeout -- a pure backstop.
            response = send_request(proc, request, timeout_seconds=limits.timeout_seconds + 10)
            return SandboxResult(**response)
        finally:
            terminate_worker(proc)
