"""Phase 17 Settings-fix regression tests, from the local Docker pass on
D:\\TradingOS-2.0\\TradingOS-2.0.

Every Settings panel returned 503 ("SECRETS_ENCRYPTION_KEY is not
configured") for two reasons that sat in deploy config, not app code:

1. The key was simply unset (no root `.env`), and the startup warning
   `scripts/api-entrypoint.sh` prints for exactly that case was invisible:
   supervisord wrote the `api` program's stdout to
   /var/log/supervisor/api.log *inside* the container, so neither the
   warning nor any request log ever reached `docker compose logs backend`.
2. Once the key was set, every saved credential would still have been
   lost on the documented `docker compose down && up --build` recreate:
   the three encrypted stores lived in the container's writable layer
   (/app/secrets) with no volume behind them.

Like test_docker_compose_network_binding.py, these read the real files at
the repo root so a future edit can't quietly reintroduce either gap.
"""

import configparser
import re
from pathlib import Path

from src.core.config import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
SUPERVISORD_CONFS = [
    REPO_ROOT / "backend" / "supervisord.conf",
    REPO_ROOT / "deploy" / "allinone-supervisord.conf",
]

SECRETS_MOUNT = "/app/secrets"
SECRETS_VOLUME = "tradingos-2.0_secrets"


def _api_program(path: Path) -> configparser.SectionProxy:
    assert path.is_file(), f"supervisord config not found at {path}"
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path, encoding="utf-8")
    assert parser.has_section("program:api"), f"{path} has no [program:api]"
    return parser["program:api"]


def test_api_logs_reach_container_stdout_in_every_supervisord_config():
    for path in SUPERVISORD_CONFS:
        api = _api_program(path)
        assert api.get("stdout_logfile") == "/dev/stdout", path
        assert api.get("stderr_logfile") == "/dev/stderr", path
        # supervisord refuses to start a program logging to a pipe unless
        # rotation is disabled -- a missing maxbytes=0 fails at boot.
        assert api.get("stdout_logfile_maxbytes") == "0", path
        assert api.get("stderr_logfile_maxbytes") == "0", path


def _backend_service_block(text: str) -> str:
    match = re.search(r"^  backend:\n(.*?)(?=^  \S)", text, re.MULTILINE | re.DOTALL)
    assert match, "backend service not found in docker-compose.yml"
    return match.group(1)


def test_backend_mounts_named_volume_over_secrets_dir():
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    backend = _backend_service_block(text)
    assert f"- {SECRETS_VOLUME}:{SECRETS_MOUNT}\n" in backend
    # Declared at top level with a fixed name, like pgdata -- otherwise
    # Compose prefixes it with the project name and a renamed checkout
    # silently starts from an empty store.
    top_level = text[text.index("\nvolumes:\n") :]
    assert f"  {SECRETS_VOLUME}:\n    name: {SECRETS_VOLUME}\n" in top_level


def test_compose_broker_store_path_is_inside_the_mounted_dir():
    backend = _backend_service_block(COMPOSE_PATH.read_text(encoding="utf-8"))
    match = re.search(r"SECRETS_STORE_PATH:\s*(\S+)", backend)
    assert match, "SECRETS_STORE_PATH not set for backend"
    assert match.group(1).startswith(SECRETS_MOUNT + "/")


def test_every_settings_store_defaults_under_secrets_dir():
    # The LLM-provider and notification-channel stores aren't overridden
    # in docker-compose.yml -- their relative defaults resolve against the
    # image's WORKDIR (/app), i.e. into the mounted /app/secrets. A new
    # store defaulting anywhere else would be wiped on recreate again.
    for field in (
        "secrets_store_path",
        "llm_provider_credentials_store_path",
        "notification_channel_store_path",
    ):
        default = Settings.model_fields[field].default
        assert default.startswith("secrets/"), (field, default)
