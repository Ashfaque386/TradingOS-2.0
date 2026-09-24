"""A pool of pre-spawned, already network-isolated sandbox worker
processes (Build Spec §9 "Warm pool"), reused across execute() calls so
repeated backtests don't each pay the cold-start cost of a fresh
`unshare --net` + Python interpreter boot that RestrictedProcessSandboxRuntime
(process_runtime.py) pays on every single call.

Each pool slot is a long-lived worker subprocess
(process_runtime.spawn_worker_process()) running worker_main.py's
stdin-read loop, which can serve many execute() calls over its lifetime --
each with its own fresh per-call CPU budget and SIGALRM timeout (see
worker_main.py's docstring for why a persistent worker needs that,
unlike a one-shot process). A worker that crashes or hangs is discarded
and replaced with a freshly-spawned one rather than reused -- a pool slot
is never left permanently broken by one bad call.
"""

import queue

from src.engine.sandbox.process_runtime import (
    SandboxWorkerCrashedError,
    SandboxWorkerHungError,
    send_request,
    spawn_worker_process,
    terminate_worker,
)
from src.engine.sandbox.types import SandboxLimits, SandboxResult


class SandboxWarmPool:
    def __init__(self, *, size: int = 2) -> None:
        self._size = size
        self._available: queue.Queue = queue.Queue()
        for _ in range(size):
            self._available.put(spawn_worker_process())

    def execute(
        self,
        code: str,
        *,
        params: dict,
        scratch_dir,
        data_dir,
        limits: SandboxLimits | None = None,
    ) -> SandboxResult:
        limits = limits or SandboxLimits()
        proc = self._available.get()
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
        try:
            response = send_request(proc, request, timeout_seconds=limits.timeout_seconds + 10)
        except (SandboxWorkerCrashedError, SandboxWorkerHungError):
            # This slot's worker is no longer trustworthy -- replace it
            # before propagating, so the pool is back at full strength for
            # the next caller rather than one permanently-short slot.
            terminate_worker(proc)
            self._available.put(spawn_worker_process())
            raise
        else:
            self._available.put(proc)  # healthy -- return it to the pool
            return SandboxResult(**response)

    def shutdown(self) -> None:
        while True:
            try:
                proc = self._available.get_nowait()
            except queue.Empty:
                break
            terminate_worker(proc)
