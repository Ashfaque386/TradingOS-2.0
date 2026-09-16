"""Encrypted local secrets store for broker credentials (Build Spec §3,
§20): "`.env` + OS-level file permissions, encrypted at rest via a
lightweight secrets file (`python-dotenv` + `cryptography` Fernet)" --
Vault is intentionally dropped for 2.0's self-hosted, single-operator
scale, so this is the whole story, not a thin shim in front of a real
secrets service.

The encryption key (`settings.secrets_encryption_key`) is a Fernet key
read from the environment/`.env` and is never written to the secrets
file alongside the ciphertext it protects. Losing that key means the
store can never be decrypted -- the correct failure mode for an
encrypted-at-rest secret; storing the key next to the ciphertext "for
convenience" would make the encryption theatre.

Every value this module returns or accepts is a `BrokerCredentials`
(src.brokers.base) whose own `__repr__` is redacted, and nothing in this
module ever passes a raw credential value to `structlog`/`logging` --
only broker names and boolean "configured" state. That, plus 0600 file
permissions on the ciphertext file, is this store's whole contract with
Build Spec §20's "never logged" requirement.
"""

import json
import os
import stat
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from src.brokers.base import BrokerCredentials
from src.core.config import get_settings


class SecretsStoreError(Exception):
    pass


class SecretsStore:
    def __init__(self, path: str | Path, encryption_key: str):
        self._path = Path(path)
        try:
            self._fernet = Fernet(encryption_key.encode("utf-8"))
        except (ValueError, TypeError) as exc:
            raise SecretsStoreError("invalid secrets encryption key") from exc

    def _read_all(self) -> dict:
        if not self._path.exists():
            return {}
        ciphertext = self._path.read_bytes()
        if not ciphertext:
            return {}
        try:
            plaintext = self._fernet.decrypt(ciphertext)
        except InvalidToken as exc:
            raise SecretsStoreError(
                f"secrets store at {self._path} could not be decrypted "
                "-- wrong encryption key or a corrupted file"
            ) from exc
        return json.loads(plaintext)

    def _write_all(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        plaintext = json.dumps(data).encode("utf-8")
        ciphertext = self._fernet.encrypt(plaintext)
        self._path.write_bytes(ciphertext)
        # Owner read/write only -- the OS-level-file-permissions half of
        # Build Spec §3's secrets-management row, on top of the
        # encryption-at-rest half above.
        os.chmod(self._path, stat.S_IRUSR | stat.S_IWUSR)

    def get_credentials(self, broker: str) -> BrokerCredentials | None:
        row = self._read_all().get(broker)
        if row is None:
            return None
        return BrokerCredentials(**row)

    def set_credentials(self, broker: str, credentials: BrokerCredentials) -> None:
        data = self._read_all()
        data[broker] = {k: v for k, v in asdict(credentials).items() if v is not None}
        self._write_all(data)

    def delete_credentials(self, broker: str) -> bool:
        data = self._read_all()
        if broker not in data:
            return False
        del data[broker]
        self._write_all(data)
        return True

    def list_configured_brokers(self) -> list[str]:
        return sorted(self._read_all().keys())


@lru_cache
def get_secrets_store() -> SecretsStore:
    settings = get_settings()
    if not settings.secrets_encryption_key:
        raise SecretsStoreError(
            "SECRETS_ENCRYPTION_KEY is not configured -- generate one with "
            '`python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"` and set it in .env'
        )
    return SecretsStore(settings.secrets_store_path, settings.secrets_encryption_key)
