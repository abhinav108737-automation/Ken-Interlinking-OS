"""Unit tests for the shared SSRF guard (ken_core.net). No DNS, no sockets."""

from __future__ import annotations

import pytest

from ken_core.net import NetworkGuard, UnsafeDestinationError, is_public_address

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"

# (address class, example) -- every one must be refused.
NON_PUBLIC = [
    ("loopback v4", "127.0.0.1"),
    ("loopback v4 other", "127.8.9.10"),
    ("loopback v6", "::1"),
    ("rfc1918 10/8", "10.1.2.3"),
    ("rfc1918 172.16/12", "172.20.0.1"),
    ("rfc1918 192.168/16", "192.168.1.1"),
    ("link-local v4", "169.254.1.1"),
    ("cloud metadata v4", "169.254.169.254"),
    ("link-local v6", "fe80::1"),
    ("link-local v6 with zone", "fe80::1%lo0"),
    ("cgnat", "100.64.0.1"),
    ("cgnat top", "100.127.255.254"),
    ("alibaba metadata", "100.100.100.200"),
    ("aws ipv6 metadata", "fd00:ec2::254"),
    ("unique-local v6", "fc00::1"),
    ("unique-local v6 fd", "fd12:3456:789a::1"),
    ("unspecified v4", "0.0.0.0"),  # noqa: S104 - a value under test, never bound
    ("this-network 0/8", "0.1.2.3"),
    ("unspecified v6", "::"),
    ("multicast v4", "224.0.0.1"),
    ("multicast v6", "ff02::1"),
    ("reserved 240/4", "240.0.0.1"),
    ("broadcast", "255.255.255.255"),
    ("benchmarking", "198.18.0.1"),
    ("documentation 192.0.2/24", "192.0.2.1"),
    ("documentation 198.51.100/24", "198.51.100.1"),
    ("documentation 203.0.113/24", "203.0.113.1"),
    ("documentation v6", "2001:db8::1"),
    ("ietf protocol 192.0.0/24", "192.0.0.8"),
    ("ipv4-mapped loopback", "::ffff:127.0.0.1"),
    ("ipv4-mapped metadata", "::ffff:169.254.169.254"),
    ("ipv4-mapped private", "::ffff:10.0.0.1"),
    ("ipv4-compatible loopback", "::127.0.0.1"),
    ("ipv4-compatible private", "::192.168.0.1"),
    ("6to4 wrapping loopback", "2002:7f00:1::1"),
    ("nat64 wrapping metadata", "64:ff9b::a9fe:a9fe"),
    ("teredo", "2001:0:4136:e378:8000:63bf:3fff:fdd2"),
]

LITERALS = [
    "http://2130706433/",  # decimal 127.0.0.1
    "http://0x7f000001/",  # hex
    "http://0177.0.0.1/",  # octal
    "http://127.1/",  # short form
    "http://0/",  # 0.0.0.0
    "http://2852039166/",  # decimal 169.254.169.254
    "http://[::ffff:127.0.0.1]/",
    "http://[::1]:8080/",
    "http://169.254.169.254/latest/meta-data/",
    "http://100.100.100.200/",
    "http://[fd00:ec2::254]/",
]


async def _no_dns(host: str, port: int | None) -> list[str]:
    raise AssertionError(f"literal {host!r} must not reach the resolver")


@pytest.mark.parametrize(("label", "address"), NON_PUBLIC, ids=[n for n, _ in NON_PUBLIC])
def test_every_non_public_class_is_refused(label: str, address: str) -> None:
    assert is_public_address(address) is False, label


@pytest.mark.parametrize("address", [PUBLIC_V4, PUBLIC_V6, "8.8.8.8", "::ffff:8.8.8.8"])
def test_public_addresses_are_allowed(address: str) -> None:
    assert is_public_address(address) is True


@pytest.mark.parametrize("url", LITERALS)
async def test_odd_ip_literals_are_judged_by_value(url: str) -> None:
    with pytest.raises(UnsafeDestinationError) as caught:
        await NetworkGuard(resolver=_no_dns).check_url(url)
    assert caught.value.code == "non_public_address"


@pytest.mark.parametrize(
    "host",
    [
        "metadata.google.internal",
        "METADATA.GOOGLE.INTERNAL.",
        "metadata",
        "instance-data",
        "anything.internal",
        "metadata.goog",
    ],
)
async def test_metadata_hostnames_refused_without_resolution(host: str) -> None:
    # even with the loopback escape hatch on, and a resolver that says "public"
    async def public(host: str, port: int | None) -> list[str]:
        return [PUBLIC_V4]

    guard = NetworkGuard(allow_private_networks=True, resolver=public)
    with pytest.raises(UnsafeDestinationError) as caught:
        await guard.check_url(f"https://{host}/")
    assert caught.value.code == "blocked_hostname"


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("ftp://example.test/", "scheme_not_allowed"),
        ("file:///etc/passwd", "scheme_not_allowed"),
        ("gopher://example.test/", "scheme_not_allowed"),
        ("http://user:pw@example.test/", "userinfo_not_allowed"),
        ("http://user@example.test/", "userinfo_not_allowed"),
        ("http://@example.test/", "userinfo_not_allowed"),
        ("http:///nohost", "host_missing"),
        ("http://example.test:99999/", "invalid_url"),
    ],
)
async def test_scheme_userinfo_and_shape_refused(url: str, code: str) -> None:
    async def public(host: str, port: int | None) -> list[str]:
        return [PUBLIC_V4]

    with pytest.raises(UnsafeDestinationError) as caught:
        await NetworkGuard(resolver=public).check_url(url)
    assert caught.value.code == code


async def test_mixed_public_and_private_records_are_refused() -> None:
    async def mixed(host: str, port: int | None) -> list[str]:
        return [PUBLIC_V4, PUBLIC_V6, "10.0.0.5"]

    with pytest.raises(UnsafeDestinationError) as caught:
        await NetworkGuard(resolver=mixed).check_url("https://mixed.test/")
    assert caught.value.code == "non_public_address"


async def test_all_public_records_are_allowed() -> None:
    async def both(host: str, port: int | None) -> list[str]:
        return [PUBLIC_V4, PUBLIC_V6]

    await NetworkGuard(resolver=both).check_url("https://public.test/path?q=1")


@pytest.mark.parametrize("failure", [OSError("nxdomain"), UnicodeError("bad label")])
async def test_resolution_failure_and_empty_answer_fail_closed(failure: Exception) -> None:
    async def boom(host: str, port: int | None) -> list[str]:
        raise failure

    async def empty(host: str, port: int | None) -> list[str]:
        return []

    for resolver in (boom, empty):
        with pytest.raises(UnsafeDestinationError) as caught:
            await NetworkGuard(resolver=resolver).check_url("https://x.test/")
        assert caught.value.code == "dns_failed"


async def test_escape_hatch_admits_loopback_only() -> None:
    strict = NetworkGuard(resolver=_no_dns)
    lenient = NetworkGuard(allow_private_networks=True, resolver=_no_dns)
    with pytest.raises(UnsafeDestinationError):
        await strict.check_url("http://127.0.0.1:8123/")
    await lenient.check_url("http://127.0.0.1:8123/")
    await lenient.check_url("http://[::1]:8123/")
    for url in (
        "http://169.254.169.254/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://100.100.100.200/",
        "http://[fd00:ec2::254]/",
    ):
        with pytest.raises(UnsafeDestinationError):
            await lenient.check_url(url)


async def test_errors_never_echo_credentials_or_urls() -> None:
    secret_urls = [
        "http://admin:hunter2@127.0.0.1/",
        "http://admin:hunter2@example.test/",
        "ftp://admin:hunter2@example.test/",
        "http://admin:hunter2@metadata.google.internal/",
    ]
    for url in secret_urls:
        with pytest.raises(UnsafeDestinationError) as caught:
            await NetworkGuard(resolver=_no_dns).check_url(url)
        text = f"{caught.value!s} {caught.value!r} {caught.value.code}"
        assert "hunter2" not in text
        assert "admin" not in text
        assert "127.0.0.1" not in text


class _Stream:
    def __init__(self, addr: object) -> None:
        self._addr = addr

    def get_extra_info(self, name: str) -> object:
        return self._addr if name == "server_addr" else None


class _Response:
    def __init__(self, stream: object | None) -> None:
        self.extensions = {} if stream is None else {"network_stream": stream}


@pytest.mark.parametrize(
    ("stream", "code"),
    [
        (_Stream(("127.0.0.1", 80)), "connected_address_non_public"),
        (_Stream(("::ffff:10.0.0.1", 80, 0, 0)), "connected_address_non_public"),
        (_Stream(("169.254.169.254", 80)), "connected_address_non_public"),
        (_Stream(None), "connected_address_unknown"),
        (_Stream(("not-an-ip", 80)), "connected_address_unknown"),
        (None, "connected_address_unknown"),
    ],
)
def test_connected_address_check(stream: object | None, code: str) -> None:
    with pytest.raises(UnsafeDestinationError) as caught:
        NetworkGuard().check_response(_Response(stream))
    assert caught.value.code == code


def test_connected_address_public_is_accepted() -> None:
    NetworkGuard().check_response(_Response(_Stream((PUBLIC_V4, 443))))
    NetworkGuard(allow_private_networks=True).check_response(_Response(_Stream(("127.0.0.1", 1))))
