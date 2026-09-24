"""Build Spec §21-22 hardening pass, item 4: the Agent Gateway's
config/CLI-mutation surface is the same single backend FastAPI app the
Console (frontend) talks to (src/api/routes/gateway.py, RBAC-gated
SystemAdministrator-only per tests/test_rbac.py's sweep) -- so the network
exposure that actually matters is docker-compose.yml's host-port
publishing for the backend and frontend services (and, for the same
"don't expose an unauthenticated or weakly-authenticated surface to the
whole network by default" reasoning, Prometheus and Grafana too).

Docker Compose's short port syntax `"HOST_PORT:CONTAINER_PORT"` binds the
host-side listener to 0.0.0.0 (every interface on the host) unless a host
IP is given explicitly (`"HOST_IP:HOST_PORT:CONTAINER_PORT"`) -- an easy
thing to lose in a future edit, since the file still "looks the same" and
still works for the default (same-machine) use case either way. This test
reads the real docker-compose.yml at the repo root and fails if any of
these four services' published ports are ever missing the
`${BIND_HOST:-127.0.0.1}:` host-IP prefix, or if a literal `0.0.0.0` shows
up anywhere in the file.
"""

import re
from pathlib import Path

# backend/tests/ -> repo root -> docker-compose.yml
COMPOSE_PATH = Path(__file__).resolve().parent.parent.parent / "docker-compose.yml"

# The exact prefix every one of these services' `ports:` entries must
# start with. Not just "must have *some* host IP" -- must specifically be
# this shared, overridable variable, so one `.env` knob (BIND_HOST) covers
# all of them consistently rather than each service silently drifting to
# its own hardcoded default over time.
REQUIRED_BIND_PREFIX = '"${BIND_HOST:-127.0.0.1}:'

# (service key as it appears in docker-compose.yml, the env var naming its
# host port) -- used only to build a readable failure message.
_GATEWAY_SURFACE_SERVICES = {
    "backend": "API_HOST_PORT",
    "frontend": "FRONTEND_HOST_PORT",
    "prometheus": "PROMETHEUS_HOST_PORT",
    "grafana": "GRAFANA_HOST_PORT",
}


def _compose_text() -> str:
    assert COMPOSE_PATH.is_file(), f"docker-compose.yml not found at {COMPOSE_PATH}"
    return COMPOSE_PATH.read_text(encoding="utf-8")


def test_docker_compose_file_exists_where_expected():
    # A silent path/rename mismatch here would make every other test in
    # this file vacuously pass (nothing to check), not fail loudly -- this
    # guards against that.
    _compose_text()


def test_gateway_surface_ports_bind_to_loopback_by_default():
    text = _compose_text()
    for env_var in _GATEWAY_SURFACE_SERVICES.values():
        pattern = re.compile(r'-\s*"([^"]*\$\{' + re.escape(env_var) + r"[^}]*\}[^\"]*)\"")
        matches = pattern.findall(text)
        assert matches, (
            f"No ports: entry referencing {env_var} found in docker-compose.yml -- "
            "has this service's port mapping been restructured? Update this test's "
            "expectations deliberately if so, don't just let it go uncovered."
        )
        for entry in matches:
            assert entry.startswith(REQUIRED_BIND_PREFIX.strip('"')), (
                f"{env_var}'s port mapping ({entry!r}) does not start with "
                f"{REQUIRED_BIND_PREFIX.strip(chr(34))!r} -- this would publish it to "
                "0.0.0.0 (every network interface on the host) by default instead of "
                "loopback-only. If this is intentional, it must still go through "
                "BIND_HOST (so `.env` still controls it), never a hardcoded IP."
            )


def test_no_hardcoded_0_0_0_0_in_any_ports_mapping():
    """Independent of the specific services checked above: a hardcoded
    0.0.0.0 literal in any `ports:` *mapping* is exactly the regression
    this whole check exists to catch, however it got there.

    Scoped to `ports:` list-item lines specifically (`      - "..."`), not
    the whole file -- a bare `--ip 0.0.0.0` on a service like Temporal's
    dev-server command is a container-*internal* bind with no `ports:`
    entry published to the host at all (reached only over the internal
    Docker network, by container hostname), which is correct and
    unrelated to host-level exposure; flagging that as if it were the same
    risk would make this check either noisy-and-ignored or force removing
    accurate documentation, neither of which is the goal.
    """
    port_mapping_lines = re.findall(r'^\s*-\s*"[^"]*"\s*$', _compose_text(), re.MULTILINE)
    assert (
        port_mapping_lines
    ), "No `ports:` mapping lines found at all -- has the file format changed?"
    offending = [line for line in port_mapping_lines if "0.0.0.0" in line]
    assert not offending, (
        f"Found a literal 0.0.0.0 in a ports: mapping: {offending!r} -- every host-port "
        "binding must go through ${BIND_HOST:-127.0.0.1} instead, so the safe "
        "loopback-only default stays a single, overridable knob."
    )


# Opt-in override that publishes the backend's embedded Postgres. It used
# to be a `"${POSTGRES_HOST_PORT:+$POSTGRES_HOST_PORT:}5432"` entry in
# docker-compose.yml itself, documented as "publishes nothing" when unset
# -- but a bare "5432" publishes on a random host port bound to 0.0.0.0
# (seen live: `0.0.0.0:53790->5432`). The checks above missed it: they
# only looked at entries naming the four known *_HOST_PORT variables.
POSTGRES_OVERRIDE_PATH = COMPOSE_PATH.parent / "docker-compose.postgres-port.yml"


def _ports_entries(text: str) -> list[str]:
    """Every list item under any `ports:` key, whatever it references."""
    entries: list[str] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() != "ports:":
            continue
        indent = len(line) - len(line.lstrip())
        for item in lines[i + 1 :]:
            stripped = item.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if len(item) - len(item.lstrip()) <= indent:
                break
            if stripped.startswith("- "):
                entries.append(stripped[2:].strip().strip("\"'"))
    return entries


def test_every_published_port_in_every_compose_file_binds_through_bind_host():
    for path in (COMPOSE_PATH, POSTGRES_OVERRIDE_PATH):
        assert path.is_file(), f"{path.name} not found at {path}"
        entries = _ports_entries(path.read_text(encoding="utf-8"))
        assert entries, f"no ports: entries parsed from {path.name}"
        for entry in entries:
            assert entry.startswith(REQUIRED_BIND_PREFIX.strip('"')), (
                f"{path.name}: ports entry {entry!r} has no ${{BIND_HOST}} host IP -- "
                "Docker publishes it on 0.0.0.0 (every interface)."
            )


def test_base_compose_never_publishes_postgres():
    entries = _ports_entries(_compose_text())
    # endswith, not ":5432": the original bug was `...:}5432`.
    assert not [e for e in entries if e.endswith("5432")], entries


def test_postgres_override_is_loopback_with_an_overridable_host_port():
    entries = _ports_entries(POSTGRES_OVERRIDE_PATH.read_text(encoding="utf-8"))
    assert entries == ["${BIND_HOST:-127.0.0.1}:${POSTGRES_HOST_PORT:-5433}:5432"]
