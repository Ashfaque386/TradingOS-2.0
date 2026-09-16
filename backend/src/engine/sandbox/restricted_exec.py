"""The trusted execution bootstrap that runs *inside* an already
network-isolated child process (see process_runtime.py -- `unshare --net`)
to actually exec() the untrusted strategy module.

This is belt-and-suspenders on top of src/engine/validation.py's AST
ban-list, not a replacement for it: real deployments run validation first
and only ever hand the sandbox code that already passed it. This module
exists for the case validation *didn't* run first, or missed something --
defense in depth, proven by tests that feed it adversarial code directly.

Two independent guards:
- A guarded `__import__` re-enforcing the same import allowlist as
  validation.py, so an import validation missed still can't resolve here.
- A path-confined `open()` restricted to a writable scratch dir and a
  read-only data-lake mount -- the one legitimate-looking filesystem
  surface AST-banning os/subprocess can't remove, since `open` is a
  builtin a real `run_backtest` might reasonably need (reading a config
  file, writing scratch diagnostics), so it has to be confined instead of
  banned outright.
"""

import builtins
from pathlib import Path

from src.engine.validation import ALLOWED_IMPORTS

_real_import = builtins.__import__


class SandboxPermissionError(PermissionError):
    """Raised for any attempt to do something the sandbox doesn't allow --
    an import outside the allowlist, or a filesystem path outside the
    scratch/data-lake mounts.
    """


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = name.split(".")[0]
    if root not in ALLOWED_IMPORTS:
        raise SandboxPermissionError(f"sandbox: import of {name!r} is not allowed")
    return _real_import(name, globals, locals, fromlist, level)


def _resolve_confined(
    path_arg: str | Path, *, scratch_dir: Path, data_dir: Path, mode: str
) -> Path:
    candidate = Path(path_arg)
    if not candidate.is_absolute():
        candidate = scratch_dir / candidate
    resolved = candidate.resolve()

    # A read mode may resolve into either mount; anything that can write
    # (w/a/x, or any "+" mode) is confined to scratch only -- the data lake
    # mount is read-only by design (Build Spec §9).
    writing = any(c in mode for c in "wax+")
    allowed_roots = [scratch_dir.resolve()]
    if not writing:
        allowed_roots.append(data_dir.resolve())

    for root in allowed_roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise SandboxPermissionError(
        f"sandbox: path {path_arg!r} (mode {mode!r}) is outside the allowed "
        "scratch/data-lake mounts"
    )


def _make_restricted_open(*, scratch_dir: Path, data_dir: Path):
    real_open = builtins.open

    def restricted_open(file, mode="r", *args, **kwargs):
        if not isinstance(file, str | Path):
            raise SandboxPermissionError(
                "sandbox: open() only accepts a path, not a file descriptor"
            )
        confined = _resolve_confined(file, scratch_dir=scratch_dir, data_dir=data_dir, mode=mode)
        return real_open(confined, mode, *args, **kwargs)

    return restricted_open


# Builtins removed outright rather than confined -- the strategy contract
# (run_backtest(data, config) -> dict) has no legitimate need for any of
# these, unlike `open`, so there's no confinement logic that would make
# sense; they're simply not present in the exec'd code's namespace.
_REMOVED_BUILTINS = frozenset({"eval", "exec", "compile", "input", "exit", "quit", "breakpoint"})


def build_restricted_globals(*, scratch_dir: Path, data_dir: Path) -> dict:
    safe_builtins = {
        name: getattr(builtins, name) for name in dir(builtins) if name not in _REMOVED_BUILTINS
    }
    safe_builtins["__import__"] = _guarded_import
    safe_builtins["open"] = _make_restricted_open(scratch_dir=scratch_dir, data_dir=data_dir)
    return {"__builtins__": safe_builtins}
