"""Sandbox worker subprocess entrypoint (Build Spec §9). Invoked as
`python3 -m src.engine.sandbox.worker_main` from inside an already
`unshare --net`-isolated child process (process_runtime.py spawns it that
way) -- this script is trusted bootstrap code, never the untrusted
strategy code it execs.

Protocol: newline-delimited JSON on stdin/stdout. One request in, one
response out, repeated for as many requests as arrive on this process's
stdin -- what makes a worker reusable across calls (src/engine/sandbox/
warm_pool.py keeps one of these alive per pool slot instead of spawning a
fresh process, and its own network-namespace/interpreter-boot cost, on
every call). The untrusted code's own stdout is captured separately (via
redirect_stdout) and never mixed into the protocol stream on real stdout.

Per-call CPU budget (Build Spec §9's "CPU/memory rlimits") is
`resource.setrlimit(RLIMIT_CPU, ...)`. RLIMIT_CPU is a *cumulative*
process-lifetime counter, not a per-call one -- naively setting it once
would give a persistent worker's Nth call a shrinking budget as earlier
calls eat into it, and could kill an otherwise-healthy warm worker after a
handful of light calls. Recomputing the limit each call as "CPU already
consumed + this call's budget" (both here and for a fresh one-shot
process, where consumed starts at ~0) makes the budget genuinely per-call
regardless of how many prior requests this worker has already served.
Wall-clock timeout is enforced separately via SIGALRM, which reliably
preempts a pure-Python CPU-bound loop (CPython checks for pending signals
between bytecode ticks) as well as anything RLIMIT_CPU wouldn't catch
(e.g. a hung blocking call that burns wall clock without CPU).
"""

import io
import json
import resource
import signal
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

from src.engine.sandbox.restricted_exec import build_restricted_globals

DEFAULT_MEMORY_BYTES = 512 * 1024 * 1024
_memory_limit_applied = False


class _SandboxTimeout(Exception):
    pass


def _alarm_handler(signum, frame):
    raise _SandboxTimeout("sandbox: execution exceeded the wall-clock timeout")


def _apply_rlimits(*, cpu_seconds: int, memory_bytes: int) -> None:
    global _memory_limit_applied
    consumed = resource.getrusage(resource.RUSAGE_SELF)
    already_used = int(consumed.ru_utime + consumed.ru_stime)
    budget = already_used + max(cpu_seconds, 1)
    resource.setrlimit(resource.RLIMIT_CPU, (budget, budget))

    # RLIMIT_AS is a live ceiling, not cumulative usage -- safe (and
    # sufficient) to apply once per worker process rather than per call.
    if not _memory_limit_applied:
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        _memory_limit_applied = True


def run_one(request: dict) -> dict:
    code = request["code"]
    params = request.get("params") or {}
    scratch_dir = Path(request["scratch_dir"])
    data_dir = Path(request["data_dir"])
    limits = request.get("limits") or {}
    timeout_seconds = int(limits.get("timeout_seconds", 30))

    scratch_dir.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    captured = io.StringIO()

    old_handler = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(timeout_seconds)
    try:
        _apply_rlimits(
            cpu_seconds=int(limits.get("cpu_seconds", 10)),
            memory_bytes=int(limits.get("memory_bytes", DEFAULT_MEMORY_BYTES)),
        )
        restricted_globals = build_restricted_globals(scratch_dir=scratch_dir, data_dir=data_dir)
        with redirect_stdout(captured):
            exec(compile(code, "<strategy>", "exec"), restricted_globals)
            run_backtest = restricted_globals.get("run_backtest")
            if run_backtest is None:
                raise RuntimeError("generated module does not define run_backtest(data, config)")
            result = run_backtest(params.get("data"), params.get("config") or {})
        json.dumps(result)  # fail fast here (not in the parent) if not JSON-serializable
        return {
            "success": True,
            "result": result,
            "error": None,
            "duration_seconds": time.monotonic() - start,
            "stdout": captured.getvalue(),
        }
    except Exception as exc:  # noqa: BLE001 - any untrusted-code failure becomes a result, not a crash
        return {
            "success": False,
            "result": None,
            "error": f"{type(exc).__name__}: {exc}",
            "duration_seconds": time.monotonic() - start,
            "stdout": captured.getvalue(),
        }
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request = json.loads(line)
        response = run_one(request)
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
