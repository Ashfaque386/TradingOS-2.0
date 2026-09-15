"""sandbox-dry-run (Build Spec §16 starter skill set). Stub: the real
gVisor/Firecracker microVM sandbox (Build Spec §9) ships in the Strategy
Pipeline & Sandbox phase. This stands in with the same result shape so
callers can be written against the real interface now.
"""


def sandbox_dry_run(params: dict) -> dict:
    return {
        "ran": True,
        "sandboxed": False,
        "exit_code": 0,
        "note": "stub: real gVisor/Firecracker sandbox ships in a later phase",
    }
