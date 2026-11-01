"""Which address a request is rate-limited under (src.core.client_ip).

The point of the module: a client must not be able to choose its own address by sending
`X-Forwarded-For`. The header is believed only from a peer the operator named, and then only the
part that peer's proxies added.
"""

import pytest

from src.core.client_ip import (
    came_over_https,
    client_ip,
    parse_networks,
    scope_came_over_https,
    scope_client_ip,
)
from src.core.config import get_settings

NONE: list = []
PROXY = parse_networks(["10.0.0.5"])
PROXY_BLOCK = parse_networks(["10.0.0.0/24", "192.168.1.1"])


def test_with_no_trusted_proxies_the_header_is_never_read():
    assert client_ip("203.0.113.9", "1.2.3.4", NONE) == "203.0.113.9"


def test_a_header_from_an_untrusted_peer_is_ignored():
    assert client_ip("203.0.113.9", "1.2.3.4", PROXY) == "203.0.113.9"


def test_a_trusted_proxy_vouches_for_the_address_it_saw():
    assert client_ip("10.0.0.5", "198.51.100.7", PROXY) == "198.51.100.7"


def test_what_the_client_put_to_the_left_of_the_proxys_entry_is_ignored():
    # The client claimed 1.2.3.4; the proxy appended the address it actually saw.
    assert client_ip("10.0.0.5", "1.2.3.4, 198.51.100.7", PROXY) == "198.51.100.7"


def test_a_chain_of_trusted_proxies_is_walked_from_the_right():
    assert client_ip("10.0.0.5", "198.51.100.7, 10.0.0.9, 192.168.1.1", PROXY_BLOCK) == (
        "198.51.100.7"
    )


def test_a_chain_made_only_of_trusted_proxies_falls_back_to_the_last_of_them():
    assert client_ip("10.0.0.5", "10.0.0.9", PROXY_BLOCK) == "10.0.0.9"


def test_a_junk_entry_stops_the_walk_at_the_last_verified_hop():
    assert client_ip("10.0.0.5", "198.51.100.7, not-an-address", PROXY) == "10.0.0.5"


def test_no_header_means_the_peer():
    assert client_ip("10.0.0.5", None, PROXY) == "10.0.0.5"
    assert client_ip("10.0.0.5", "", PROXY) == "10.0.0.5"


def test_ipv6_addresses_work_in_peer_header_and_blocks():
    trusted = parse_networks(["fd00::/8"])

    assert client_ip("fd00::5", "2001:db8::7", trusted) == "2001:db8::7"
    assert client_ip("2001:db8::9", "1.2.3.4", trusted) == "2001:db8::9"


def test_a_peer_that_is_not_an_address_is_returned_as_given():
    assert client_ip("unix-socket", "1.2.3.4", PROXY) == "unix-socket"
    assert client_ip(None, "1.2.3.4", PROXY) == "unknown"


def test_a_setting_that_is_not_an_address_is_an_error_not_silent_trust_of_nothing():
    with pytest.raises(ValueError, match="TRUSTED_PROXIES"):
        parse_networks(["10.0.0.5", "banana"])


def test_the_complaint_names_the_setting_and_the_entry():
    with pytest.raises(
        ValueError, match=r"^TRUSTED_PROXIES: 'banana' is not an address or a CIDR block$"
    ):
        parse_networks(["banana"])


def test_a_host_written_with_a_prefix_is_read_as_the_block_it_belongs_to():
    # 10.0.0.5/24 has host bits set; the operator meant their proxy's network, not an error.
    assert parse_networks(["10.0.0.5/24"]) == parse_networks(["10.0.0.0/24"])


# ---- https ----


def test_a_tls_connection_is_https_whoever_sent_it():
    assert came_over_https("https", "203.0.113.9", None, NONE)
    assert came_over_https("wss", "203.0.113.9", None, NONE)


def test_a_plain_connection_is_https_only_when_a_trusted_proxy_says_so():
    assert not came_over_https("http", "203.0.113.9", "https", NONE)
    assert not came_over_https("http", "203.0.113.9", "https", PROXY)
    assert came_over_https("http", "10.0.0.5", "https", PROXY)
    assert not came_over_https("http", "10.0.0.5", "http", PROXY)
    assert not came_over_https("http", "10.0.0.5", None, PROXY)


def test_the_first_protocol_in_a_list_is_the_one_nearest_the_client():
    assert came_over_https("http", "10.0.0.5", "https, http", PROXY)
    assert not came_over_https("http", "10.0.0.5", "http, https", PROXY)


# ---- from an ASGI scope, with the configured proxies ----


def _scope(peer: str, forwarded_for: str | None = None, proto: str | None = None):
    headers = []
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode()))
    if proto is not None:
        headers.append((b"x-forwarded-proto", proto.encode()))
    return {"client": (peer, 5000), "headers": headers, "scheme": "http"}


def test_the_scope_helpers_read_the_configured_proxies(monkeypatch):
    scope = _scope("10.0.0.5", "198.51.100.7", "https")

    assert scope_client_ip(scope) == "10.0.0.5"
    assert not scope_came_over_https(scope)

    monkeypatch.setattr(get_settings(), "trusted_proxies", "10.0.0.5")

    assert scope_client_ip(scope) == "198.51.100.7"
    assert scope_came_over_https(scope)


def test_several_forwarded_for_header_lines_are_joined_in_order(monkeypatch):
    monkeypatch.setattr(get_settings(), "trusted_proxies", "10.0.0.5")
    scope = {
        "client": ("10.0.0.5", 1),
        "headers": [
            (b"x-forwarded-for", b"1.2.3.4"),
            (b"x-forwarded-for", b"198.51.100.7"),
        ],
        "scheme": "http",
    }

    assert scope_client_ip(scope) == "198.51.100.7"


def test_a_scope_over_tls_is_https_and_one_that_says_nothing_about_its_scheme_is_not():
    assert scope_came_over_https({"client": ("203.0.113.9", 1), "headers": [], "scheme": "https"})
    assert not scope_came_over_https({"client": ("203.0.113.9", 1), "headers": []})


def test_a_scope_with_no_client_has_no_address():
    assert scope_client_ip({"headers": [], "scheme": "http"}) == "unknown"
