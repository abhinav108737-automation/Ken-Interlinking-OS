"""SSRF guard (QA review): :class:`NetworkGuard`.

A URL that came from content an attacker may influence (a crawled page, a CMS link target)
must never make this server talk to a loopback, private, link-local or metadata address.
The guard has three parts, all fail-closed:

1. :meth:`NetworkGuard.check_url` -- before any I/O. Scheme must be http(s); no userinfo; a
   metadata HOSTNAME is refused outright; the host (odd IP literals normalised through
   ``inet_aton``, otherwise ALL A/AAAA records via ``getaddrinfo`` in a thread) must resolve
   only to public addresses. One bad record refuses the whole host.
2. :meth:`NetworkGuard.check_response` -- after the response headers arrive and BEFORE the body
   is read: the address the socket ACTUALLY connected to must be public. Resolve-then-connect
   alone is a time-of-check/time-of-use gap (DNS rebinding); this closes it. The request was
   already sent, so the guarantee is "the response is discarded", not "nothing was sent".
3. Callers follow redirects by hand and call :meth:`check_url` on every hop.

Errors are :class:`UnsafeDestinationError` with a FIXED ``code``; the message never contains
the URL (it may carry credentials). ``allow_private_networks`` is a test/local-only escape
hatch that admits LOOPBACK addresses only (never link-local, private or metadata ones); the
settings layer refuses it outside the ``local`` and ``ci`` environments.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from typing import Any, Final
from urllib.parse import urlsplit

__all__ = ["NetworkGuard", "Resolver", "UnsafeDestinationError", "is_public_address"]

type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
type Resolver = Callable[[str, int | None], Awaitable[list[str]]]

CODES: Final = (
    "scheme_not_allowed",
    "userinfo_not_allowed",
    "host_missing",
    "invalid_url",
    "blocked_hostname",
    "dns_failed",
    "non_public_address",
    "connected_address_non_public",
    "connected_address_unknown",
)

_METADATA_HOSTS: Final = frozenset(
    {
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "instance-data.ec2.internal",
        "kubernetes.default.svc",
    }
)
_BLOCKED_SUFFIXES: Final = (".internal", ".metadata", ".localdomain")

# What ``is_global`` misses or gets wrong across Python versions, denied explicitly.
_DENY_NETWORKS: Final = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",  # CGNAT
        "100.100.100.200/32",  # Alibaba metadata
        "127.0.0.0/8",
        "169.254.0.0/16",  # link-local incl. 169.254.169.254
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",  # benchmarking
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "::1/128",
        "100::/64",
        "2001::/32",  # Teredo
        "2001:db8::/32",
        "fc00::/7",
        "fd00:ec2::254/128",  # AWS IPv6 metadata
        "fe80::/10",
        "fec0::/10",
        "ff00::/8",
    )
)
_LOOPBACK: Final = (ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128"))


_NAT64: Final = ipaddress.ip_network("64:ff9b::/96")
_SIX_TO_FOUR: Final = ipaddress.ip_network("2002::/16")


def _embedded_v4(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address hidden in a mapped / compatible / 6to4 / NAT64 IPv6 address."""
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    value = int(address)
    if value >> 32 == 0:  # ::a.b.c.d (IPv4-compatible, deprecated)
        return ipaddress.IPv4Address(value & 0xFFFFFFFF)
    if address in _NAT64:
        return ipaddress.IPv4Address(value & 0xFFFFFFFF)
    if address in _SIX_TO_FOUR:
        return ipaddress.IPv4Address((value >> 80) & 0xFFFFFFFF)
    return None


def _strip_zone(text: str) -> str:
    return text.split("%", 1)[0]


def is_public_address(address: IPAddress | str) -> bool:
    """True only for a globally routable unicast address (embedded IPv4 judged too)."""
    ip = ipaddress.ip_address(_strip_zone(address) if isinstance(address, str) else address)
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = _embedded_v4(ip)
        if embedded is not None:
            return is_public_address(embedded)
    if any(ip in net for net in _DENY_NETWORKS if net.version == ip.version):
        return False
    return bool(ip.is_global) and not (
        ip.is_multicast or ip.is_reserved or ip.is_unspecified or ip.is_private
    )


def _is_loopback(address: IPAddress) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in net for net in _LOOPBACK if net.version == address.version)


async def _default_resolver(host: str, port: int | None) -> list[str]:
    infos = await asyncio.to_thread(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


class UnsafeDestinationError(Exception):
    """A destination the guard refuses. ``code`` is one of :data:`CODES`; no URL inside."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _normalise_literal(host: str) -> str | None:
    """A dotted-quad for any inet_aton-style IPv4 literal (decimal, hex, octal, short)."""
    if not host or not all(c in "0123456789abcdefxABCDEFX." for c in host):
        return None
    try:
        return socket.inet_ntoa(socket.inet_aton(host))
    except OSError:
        return None


class NetworkGuard:
    """Judges destinations; see the module docstring."""

    def __init__(
        self, *, allow_private_networks: bool = False, resolver: Resolver | None = None
    ) -> None:
        self._allow_loopback = allow_private_networks
        self._resolver: Resolver = resolver or _default_resolver

    def _admit(self, address: IPAddress) -> bool:
        return is_public_address(address) or (self._allow_loopback and _is_loopback(address))

    async def check_url(self, url: str) -> None:
        """Raise :class:`UnsafeDestinationError` unless every address ``url`` resolves to is OK."""
        try:
            parts = urlsplit(url)
            port = parts.port
            host = parts.hostname
        except ValueError:
            raise UnsafeDestinationError("invalid_url") from None
        if parts.scheme.lower() not in {"http", "https"}:
            raise UnsafeDestinationError("scheme_not_allowed")
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            raise UnsafeDestinationError("userinfo_not_allowed")
        if not host:
            raise UnsafeDestinationError("host_missing")
        host = host.lower().rstrip(".")
        if host in _METADATA_HOSTS or host.endswith(_BLOCKED_SUFFIXES):
            raise UnsafeDestinationError("blocked_hostname")
        literal = _normalise_literal(host)
        candidates: list[str]
        if literal is not None:
            candidates = [literal]
        else:
            try:
                ipaddress.ip_address(_strip_zone(host))
                candidates = [host]
            except ValueError:
                try:
                    candidates = await self._resolver(host, port)
                except (OSError, UnicodeError):
                    raise UnsafeDestinationError("dns_failed") from None
        if not candidates:
            raise UnsafeDestinationError("dns_failed")
        for candidate in candidates:
            try:
                address = ipaddress.ip_address(_strip_zone(candidate))
            except ValueError:
                raise UnsafeDestinationError("dns_failed") from None
            if not self._admit(address):
                raise UnsafeDestinationError("non_public_address")

    def check_response(self, response: Any) -> None:
        """Refuse a response whose socket peer is not an admitted address (DNS rebinding)."""
        stream = getattr(response, "extensions", {}).get("network_stream")
        info = stream.get_extra_info("server_addr") if stream is not None else None
        if not info:
            raise UnsafeDestinationError("connected_address_unknown")
        try:
            address = ipaddress.ip_address(_strip_zone(str(info[0])))
        except (ValueError, IndexError, TypeError):
            raise UnsafeDestinationError("connected_address_unknown") from None
        if not self._admit(address):
            raise UnsafeDestinationError("connected_address_non_public")
