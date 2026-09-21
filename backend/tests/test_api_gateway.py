"""Agent Gateway config HTTP routes (Build Spec §6, §21-22 hardening pass).

GET /gateway/config used to re-read and re-validate the config file
straight off disk on every call (`load_and_validate(path)`), instead of
serving the same in-memory last-known-good `GatewayState` every
agent-facing call site reads (src/gateway/state.py's own module
docstring: "the app keeps running on whatever was here before"). Found
via the Playwright E2E hot-reload spec (e2e/agent-gateway-hot-reload.spec.ts):
after hand-editing config/tradingos.config.json with invalid JSON5, the
Settings page itself -- exactly where an operator would look to confirm
the app is still fine -- returned a 500 instead of showing the still-
running last-known-good config, directly contradicting that file's own
top-of-file comment to a human operator ("the app keeps running on the
last-known-good config").
"""

from pathlib import Path

from httpx import AsyncClient

from src.core.roles import Role
from src.gateway.apply import apply_config_from_file
from src.gateway.state import get_state
from src.models.agent_config_version import ConfigVersionStatus

_VALID_CONFIG = """
{
  version: 1,
  infra: {
    llmProviders: { order: ['anthropic'] },
    brokerFailover: { primary: 'zerodha', fallback: 'upstox' },
    riskThresholdRefs: { maxDrawdownPct: 15, wsLatencyMs: 100 },
  },
  agents: {
    entries: {
      'ceo-agent': { identity: { name: 'CEO', emoji: '\U0001f9e0', theme: 'cyan' } },
    },
  },
}
"""


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def test_get_config_serves_last_known_good_even_when_the_file_on_disk_is_broken(
    client, make_user, db_session_factory, tmp_path: Path
):
    config_path = tmp_path / "tradingos.config.json"
    config_path.write_text(_VALID_CONFIG)

    async with db_session_factory() as session:
        result = await apply_config_from_file(session, config_path, source="test-seed")
    assert result.status == ConfigVersionStatus.ACTIVE
    assert get_state().get_version_id() == result.version_id

    # The exact scenario a rejected hot-reload leaves behind: the file on
    # disk is now broken, but nothing about "what's actually running"
    # should be affected by that -- GatewayState was never touched.
    config_path.write_text("{ this is not valid json5 !!! ")

    await make_user("gw-admin@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "gw-admin@example.com", "supersecret1")

    resp = await client.get("/api/v1/gateway/config", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version_id"] == result.version_id
    assert body["parsed"]["agents"]["entries"]["ceo-agent"]["identity"]["theme"] == "cyan"
    assert "version: 1" in body["raw_text"]


async def test_get_config_returns_503_before_any_config_has_ever_been_applied(
    client, make_user, db_session_factory
):
    # A clean process before `apply_config_from_file` has ever run (the
    # real app always applies one during startup, but the endpoint itself
    # must not assume that -- a missing state is an honest 503, never a
    # crash or a fabricated empty config). GatewayState is a process-wide
    # singleton (other tests, e.g. test_gateway_hotreload.py, rely on that
    # to observe a real watcher's effect) so this test saves and restores
    # it rather than leaving it null for whatever runs next.
    state = get_state()
    saved_config, saved_version_id = state._config, state._version_id  # noqa: SLF001
    state._config = None  # noqa: SLF001
    state._version_id = None  # noqa: SLF001
    try:
        await make_user("gw-admin2@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        token = await _login(client, "gw-admin2@example.com", "supersecret1")

        resp = await client.get(
            "/api/v1/gateway/config", headers={"Authorization": f"Bearer {token}"}
        )

        assert resp.status_code == 503
    finally:
        state._config, state._version_id = saved_config, saved_version_id  # noqa: SLF001
