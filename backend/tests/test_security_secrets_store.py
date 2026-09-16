"""Encrypted secrets store tests (Build Spec §3, §20): a round trip
through the real Fernet encryption, wrong-key failure, 0600 file
permissions, and -- the load-bearing assertion -- that the raw secret
value never appears in the on-disk ciphertext file.
"""

import stat

import pytest
from cryptography.fernet import Fernet

from src.brokers.base import BrokerCredentials
from src.security.secrets_store import SecretsStore, SecretsStoreError


def _make_store(tmp_path, key: str | None = None) -> SecretsStore:
    return SecretsStore(tmp_path / "secrets.enc", key or Fernet.generate_key().decode())


def test_set_then_get_round_trips(tmp_path):
    store = _make_store(tmp_path)
    creds = BrokerCredentials(api_key="AKEY123", api_secret="ASECRET456", access_token="ATOKEN789")
    store.set_credentials("zerodha", creds)

    fetched = store.get_credentials("zerodha")
    assert fetched == creds


def test_get_missing_broker_returns_none(tmp_path):
    store = _make_store(tmp_path)
    assert store.get_credentials("zerodha") is None


def test_delete_removes_credentials(tmp_path):
    store = _make_store(tmp_path)
    store.set_credentials("upstox", BrokerCredentials(api_key="K"))
    assert store.delete_credentials("upstox") is True
    assert store.get_credentials("upstox") is None
    assert store.delete_credentials("upstox") is False


def test_list_configured_brokers(tmp_path):
    store = _make_store(tmp_path)
    store.set_credentials("zerodha", BrokerCredentials(api_key="K1"))
    store.set_credentials("upstox", BrokerCredentials(api_key="K2"))
    assert store.list_configured_brokers() == ["upstox", "zerodha"]


def test_wrong_key_cannot_decrypt(tmp_path):
    key_a = Fernet.generate_key().decode()
    key_b = Fernet.generate_key().decode()
    store_a = SecretsStore(tmp_path / "secrets.enc", key_a)
    store_a.set_credentials("zerodha", BrokerCredentials(api_key="SECRET_VALUE"))

    store_b = SecretsStore(tmp_path / "secrets.enc", key_b)
    with pytest.raises(SecretsStoreError):
        store_b.get_credentials("zerodha")


def test_invalid_key_format_raises_at_construction(tmp_path):
    with pytest.raises(SecretsStoreError):
        SecretsStore(tmp_path / "secrets.enc", "not-a-valid-fernet-key")


def test_file_permissions_are_owner_only(tmp_path):
    store = _make_store(tmp_path)
    store.set_credentials("zerodha", BrokerCredentials(api_key="K"))

    path = tmp_path / "secrets.enc"
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == stat.S_IRUSR | stat.S_IWUSR


def test_raw_secret_value_never_appears_in_the_ciphertext_file(tmp_path):
    store = _make_store(tmp_path)
    secret_marker = "SUPER_SECRET_TOKEN_VALUE_DO_NOT_LEAK"
    store.set_credentials("zerodha", BrokerCredentials(api_key="K", access_token=secret_marker))

    raw_bytes = (tmp_path / "secrets.enc").read_bytes()
    assert secret_marker.encode("utf-8") not in raw_bytes


def test_credentials_repr_never_includes_raw_values():
    creds = BrokerCredentials(
        api_key="RAWKEYVALUE", api_secret="RAWSECRETVALUE", access_token="RAWTOKENVALUE"
    )
    text = repr(creds)
    assert "RAWKEYVALUE" not in text
    assert "RAWSECRETVALUE" not in text
    assert "RAWTOKENVALUE" not in text
    assert "redacted" in text
