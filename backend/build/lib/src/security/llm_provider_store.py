"""Encrypted local store for LLM provider credentials (Settings redesign) --
the sibling this codebase never had for src.security.secrets_store's broker
credentials: until now, provider API keys were env-var-only
(src.core.config.Settings.anthropic_api_key etc.), with no runtime,
UI-editable, encrypted-at-rest store, and the Settings page said so
explicitly. Same Fernet-encryption-at-rest + 0600-file-permissions + never-
logged contract as secrets_store.py, kept as an independent file (own
settings.llm_provider_credentials_store_path) rather than folded into that
module, matching this codebase's existing convention of one store per
secret domain (see src.notifications.channel_store for the same pattern
applied to notification channels).
"""

import json
import os
import stat
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from src.core.config import get_settings


class LlmProviderStoreError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class LlmProviderCredentials:
    """api_key is None for a provider that needs only base_url (Custom/
    Local, e.g. Ollama) -- never required, never assumed. base_url is safe
    to echo back in a status response (it's an endpoint, not a secret);
    api_key never is."""

    api_key: str | None = None
    base_url: str | None = None
    # The model an agent's `model: "auto"` resolves to on this provider
    # (src.agents.llm_router.resolve_model). Required for providers with
    # no universal default (Ollama, Custom, Hugging Face); optional
    # override of the built-in default for the other hosted ones.
    default_model: str | None = None

    def __repr__(self) -> str:
        present = []
        if self.api_key:
            present.append("api_key")
        if self.base_url:
            present.append("base_url")
        return f"LlmProviderCredentials(<redacted: {', '.join(present)}>)"


class LlmProviderStore:
    def __init__(self, path: str | Path, encryption_key: str):
        self._path = Path(path)
        try:
            self._fernet = Fernet(encryption_key.encode("utf-8"))
        except (ValueError, TypeError) as exc:
            raise LlmProviderStoreError("invalid secrets encryption key") from exc

    def _read_all(self) -> dict:
        if not self._path.exists():
            return {}
        ciphertext = self._path.read_bytes()
        if not ciphertext:
            return {}
        try:
            plaintext = self._fernet.decrypt(ciphertext)
        except InvalidToken as exc:
            raise LlmProviderStoreError(
                f"LLM provider store at {self._path} could not be decrypted "
                "-- wrong encryption key or a corrupted file"
            ) from exc
        return json.loads(plaintext)

    def _write_all(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        plaintext = json.dumps(data).encode("utf-8")
        ciphertext = self._fernet.encrypt(plaintext)
        self._path.write_bytes(ciphertext)
        os.chmod(self._path, stat.S_IRUSR | stat.S_IWUSR)

    def get_credentials(self, provider: str) -> LlmProviderCredentials | None:
        row = self._read_all().get(provider)
        if row is None:
            return None
        return LlmProviderCredentials(**row)

    def set_credentials(self, provider: str, credentials: LlmProviderCredentials) -> None:
        data = self._read_all()
        data[provider] = {k: v for k, v in asdict(credentials).items() if v is not None}
        self._write_all(data)

    def delete_credentials(self, provider: str) -> bool:
        data = self._read_all()
        if provider not in data:
            return False
        del data[provider]
        self._write_all(data)
        return True

    def list_configured_providers(self) -> list[str]:
        return sorted(self._read_all().keys())


@lru_cache
def get_llm_provider_store() -> LlmProviderStore:
    settings = get_settings()
    if not settings.secrets_encryption_key:
        raise LlmProviderStoreError(
            "SECRETS_ENCRYPTION_KEY is not configured -- generate one with "
            '`python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"` and set it in .env'
        )
    return LlmProviderStore(
        settings.llm_provider_credentials_store_path, settings.secrets_encryption_key
    )
