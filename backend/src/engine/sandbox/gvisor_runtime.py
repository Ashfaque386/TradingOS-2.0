"""Production sandbox runtime (Build Spec §9): executes strategy code
inside a gVisor (`runsc`) container instead of a bare subprocess -- "true
kernel-level isolation for untrusted, LLM-generated code."

**Why gVisor over Firecracker** (the other option Build Spec §9 names):
gVisor intercepts syscalls in userspace (its ptrace platform, or KVM where
available) and needs no hardware virtualization support to run at all.
Firecracker's jailer+VMM model hard-requires `/dev/kvm` -- confirmed absent
in this project's own dev/CI sandbox, and commonly absent in other
nested/cloud container environments this app might run in, which would
make Firecracker simply not work in the same class of environment gVisor
tolerates. gVisor also registers as a drop-in OCI/Docker runtime
(`docker run --runtime=runsc`), which fits this project's existing
single-container deployment story (Dockerfile, docker-compose.yml) far
more naturally than Firecracker's separate VMM-plus-jailer supervisor
process model would.

**Never exercised in this repo's own tests or CI**: this dev sandbox has
no `runsc` installed and no Docker runtime registered for it (confirmed
via `docker info` -- see is_gvisor_available() below, and the fact that
constructing this class raises SandboxRuntimeUnavailableError here every
time). RestrictedProcessSandboxRuntime (process_runtime.py) is what
actually runs in this repo's tests -- same "real code path, honestly
unexercised without the real infra" posture as llm_router.py's provider
HTTP clients (Phase 3), which also have no credentials/egress to run
against in this sandbox.
"""

import json
import subprocess
import time
from pathlib import Path

from src.engine.sandbox.types import SandboxLimits, SandboxResult, SandboxRuntimeUnavailableError

GVISOR_RUNTIME_NAME = "runsc"

_WORKER_ENTRYPOINT = (
    "import json,sys; sys.path.insert(0, '/app'); "
    "from src.engine.sandbox.worker_main import run_one; "
    "print(json.dumps(run_one(json.load(open('/scratch/_request.json')))))"
)


def is_gvisor_available() -> bool:
    try:
        info = subprocess.run(
            ["docker", "info", "--format", "{{json .Runtimes}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return info.returncode == 0 and GVISOR_RUNTIME_NAME in info.stdout


class GvisorSandboxRuntime:
    """`docker run --runtime=runsc --network=none --read-only` per call,
    with the scratch dir bind-mounted read-write and the data lake
    bind-mounted read-only -- gVisor's own kernel-level syscall
    interception is the isolation boundary here (not the Python-level
    guards process_runtime.py relies on, though worker_main.py's
    restricted globals are still applied inside the container too, as a
    second layer).
    """

    def __init__(self, *, image: str = "tradingos-sandbox-worker:latest") -> None:
        self._image = image
        if not is_gvisor_available():
            raise SandboxRuntimeUnavailableError(
                "gVisor is not registered as a Docker runtime on this host "
                "(`docker info` does not list 'runsc'). Install gVisor and "
                "register it (https://gvisor.dev/docs/user_guide/install/), "
                "or use RestrictedProcessSandboxRuntime instead."
            )

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
        scratch_dir.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)

        request_path = scratch_dir / "_request.json"
        request_path.write_text(
            json.dumps(
                {
                    "code": code,
                    "params": params,
                    "scratch_dir": "/scratch",
                    "data_dir": "/data",
                    "limits": {
                        "cpu_seconds": limits.cpu_seconds,
                        "memory_bytes": limits.memory_bytes,
                        "timeout_seconds": limits.timeout_seconds,
                    },
                }
            )
        )

        cmd = [
            "docker",
            "run",
            "--rm",
            "--runtime",
            GVISOR_RUNTIME_NAME,
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--cpus",
            "1",
            "--memory",
            str(limits.memory_bytes),
            "-v",
            f"{scratch_dir}:/scratch:rw",
            "-v",
            f"{data_dir}:/data:ro",
            self._image,
            "python3",
            "-c",
            _WORKER_ENTRYPOINT,
        ]
        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=limits.timeout_seconds + 10
            )
        except subprocess.TimeoutExpired:
            return SandboxResult(
                success=False,
                error="gVisor container did not exit within the parent-side timeout backstop",
                duration_seconds=time.monotonic() - start,
            )

        if proc.returncode != 0:
            return SandboxResult(
                success=False,
                error=f"gVisor container exited {proc.returncode}: {proc.stderr.strip()}",
                duration_seconds=time.monotonic() - start,
            )
        response = json.loads(proc.stdout)
        return SandboxResult(**response)
