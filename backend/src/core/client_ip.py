"""Which address a request really came from, and whether it came over HTTPS.

A rate limit keyed by address is only as good as the address. `X-Forwarded-For` is a header the
client can send itself, so believing it unconditionally lets anyone choose the address they are
limited under (a fresh one per request is enough to never be limited). It is believed here only when
the TCP peer is one of the reverse proxies the operator named (`TRUSTED_PROXIES`), and then only the
part those proxies added: the list is read from the right, skipping trusted proxies, and the first
address that is not one is the client. Whatever the client put to the left of that is ignored.

With no trusted proxies configured (the default) the header is never read.
"""

import ipaddress
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from src.core.config import get_settings

Network = ipaddress.IPv4Network | ipaddress.IPv6Network
Address = ipaddress.IPv4Address | ipaddress.IPv6Address


def parse_networks(entries: Iterable[str]) -> list[Network]:
    """Addresses and CIDR blocks from the setting. An entry that is not one is a configuration
    error and says so, rather than silently trusting nothing (or, worse, something else)."""
    networks: list[Network] = []
    for entry in entries:
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError as exc:
            raise ValueError(
                f"TRUSTED_PROXIES: {entry!r} is not an address or a CIDR block"
            ) from exc
    return networks


def _parse_address(text: str | None) -> Address | None:
    if not text:
        return None
    try:
        return ipaddress.ip_address(text.strip())
    except ValueError:
        return None


def _is_trusted(address: Address, trusted: Sequence[Network]) -> bool:
    return any(address in network for network in trusted)


def client_ip(peer: str | None, forwarded_for: str | None, trusted: Sequence[Network]) -> str:
    """The address to rate-limit and log a request under."""
    peer_address = _parse_address(peer)
    if peer_address is None:
        return peer or "unknown"
    if not forwarded_for or not _is_trusted(peer_address, trusted):
        return str(peer_address)

    current = peer_address
    for entry in reversed(forwarded_for.split(",")):
        address = _parse_address(entry)
        if address is None:
            # Something that is not an address was put in the chain: believe nothing beyond the last
            # hop that was verified.
            return str(current)
        if _is_trusted(address, trusted):
            current = address
            continue
        return str(address)
    return str(current)


def _header_values(scope: Mapping[str, Any], name: bytes) -> str | None:
    values = [v.decode("latin-1") for k, v in (scope.get("headers") or []) if k == name]
    return ", ".join(values) if values else None


def scope_client_ip(scope: Mapping[str, Any]) -> str:
    """`client_ip` for an ASGI scope, using the configured trusted proxies."""
    client = scope.get("client")
    peer = client[0] if client else None
    return client_ip(
        peer,
        _header_values(scope, b"x-forwarded-for"),
        parse_networks(get_settings().trusted_proxies_list),
    )


def scope_came_over_https(scope: Mapping[str, Any]) -> bool:
    client = scope.get("client")
    return came_over_https(
        str(scope.get("scheme", "http")),
        client[0] if client else None,
        _header_values(scope, b"x-forwarded-proto"),
        parse_networks(get_settings().trusted_proxies_list),
    )


def came_over_https(
    scheme: str, peer: str | None, forwarded_proto: str | None, trusted: Sequence[Network]
) -> bool:
    """True when the connection itself was TLS, or a trusted proxy says the client's was."""
    if scheme in ("https", "wss"):
        return True
    peer_address = _parse_address(peer)
    if peer_address is None or not _is_trusted(peer_address, trusted) or not forwarded_proto:
        return False
    # With several proxies the header can be a list; the one nearest the client is first.
    return forwarded_proto.split(",")[0].strip().lower() == "https"
