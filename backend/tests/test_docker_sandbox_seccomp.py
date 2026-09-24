"""Phase 17 local Docker pass: every POST /api/v1/strategies 500'd in the
Docker stack with `SandboxWorkerCrashedError: unshare: unshare failed:
Operation not permitted`. A bare `unshare --net` needs CAP_SYS_ADMIN, which
Docker withholds, and Docker's default seccomp profile blocks unshare()
outright. The fix: the worker creates its network namespace inside a fresh
user namespace (no capability needed), and the backend runs under Docker's
default seccomp profile plus one rule allowing unshare() for user+net
namespaces only. Verified live (see docs/phase17-realworld-testing.md):
network unreachable inside the worker, mount/pid namespaces still denied.

These tests pin that config. The live isolation proof itself is
test_engine_sandbox.py, which runs whatever UNSHARE_NET_ARGS is.
"""

import json
from pathlib import Path

from src.engine.sandbox.process_runtime import UNSHARE_NET_ARGS

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROFILE_PATH = REPO_ROOT / "deploy" / "seccomp" / "backend.json"
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"

CLONE_NEWUSER = 0x10000000
CLONE_NEWNET = 0x40000000
ALL_CLONE_NEW_FLAGS = 0x7E020000


def _profile() -> dict:
    assert PROFILE_PATH.is_file(), f"seccomp profile not found at {PROFILE_PATH}"
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def test_sandbox_uses_a_user_namespace_for_its_network_namespace():
    assert UNSHARE_NET_ARGS[0] == "unshare"
    assert {"--user", "--map-root-user", "--net"} <= set(UNSHARE_NET_ARGS)
    assert UNSHARE_NET_ARGS[-1] == "--"


def test_profile_is_default_deny():
    assert _profile()["defaultAction"] == "SCMP_ACT_ERRNO"


def test_only_unshare_is_ungated_and_only_for_user_and_net_namespaces():
    syscalls = _profile()["syscalls"]
    ungated_unshare = [
        rule
        for rule in syscalls
        if "unshare" in rule["names"]
        and rule["action"] == "SCMP_ACT_ALLOW"
        and not rule.get("includes", {}).get("caps")
    ]
    assert len(ungated_unshare) == 1
    rule = ungated_unshare[0]
    assert rule["names"] == ["unshare"]
    (arg,) = rule["args"]
    assert arg["index"] == 0
    assert arg["op"] == "SCMP_CMP_MASKED_EQ"
    assert arg.get("valueTwo", 0) == 0
    # The mask must forbid every namespace flag except user and network.
    assert arg["value"] == ALL_CLONE_NEW_FLAGS & ~(CLONE_NEWUSER | CLONE_NEWNET)

    # Nothing else from Docker's CAP_SYS_ADMIN-gated group (mount, setns,
    # clone with namespace flags, ...) was opened up.
    for other in syscalls:
        if other is rule or other["action"] != "SCMP_ACT_ALLOW":
            continue
        if other.get("includes", {}).get("caps"):
            continue
        assert not {"mount", "setns", "umount2", "clone3"} & set(other["names"]), other


def test_backend_service_runs_under_the_profile_without_extra_caps():
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    assert "- seccomp=./deploy/seccomp/backend.json\n" in text
    config_lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    assert not any("cap_add" in line or "SYS_ADMIN" in line for line in config_lines)
    assert not any("privileged:" in line for line in config_lines)
