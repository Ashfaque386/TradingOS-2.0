"""Shared types for the strategy sandbox (Build Spec §9)."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    cpu_seconds: int = 10
    memory_bytes: int = 512 * 1024 * 1024
    timeout_seconds: int = 30


@dataclass(frozen=True, slots=True)
class SandboxResult:
    success: bool
    result: dict | None = None
    error: str | None = None
    duration_seconds: float = 0.0
    stdout: str = ""


class SandboxError(Exception):
    """Base for every sandbox execution failure."""


class SandboxRuntimeUnavailableError(SandboxError):
    """Raised when a runtime's underlying isolation mechanism isn't present
    on this host (e.g. gVisor not registered as a Docker runtime). Same
    honest-failure posture as llm_router's "no API key configured" --
    callers never silently fall back to a weaker guarantee behind this
    runtime's back; a caller wanting a fallback picks a different runtime
    explicitly (see src/engine/sandbox/factory.py).
    """
