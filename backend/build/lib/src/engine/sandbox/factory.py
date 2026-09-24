"""Picks the best sandbox runtime actually available on this host: gVisor
in production where it's installed and registered, the always-available
restricted-process runtime everywhere else (this repo's own dev/CI
environment included). Callers that need a specific runtime regardless
(e.g. this repo's own tests, which need determinism) construct
RestrictedProcessSandboxRuntime directly instead of going through this.
"""

from pathlib import Path
from typing import Protocol

from src.engine.sandbox.gvisor_runtime import GvisorSandboxRuntime, is_gvisor_available
from src.engine.sandbox.process_runtime import RestrictedProcessSandboxRuntime
from src.engine.sandbox.types import SandboxLimits, SandboxResult


class SandboxRuntime(Protocol):
    def execute(
        self,
        code: str,
        *,
        params: dict,
        scratch_dir: Path,
        data_dir: Path,
        limits: SandboxLimits | None = None,
    ) -> SandboxResult: ...


def get_default_sandbox_runtime() -> SandboxRuntime:
    if is_gvisor_available():
        return GvisorSandboxRuntime()
    return RestrictedProcessSandboxRuntime()
