"""URL normalisation and site scope (ken_services.inventory.urls, frozen helper)."""

from __future__ import annotations

import pytest

from ken_contracts.inventory import MAX_URL_LENGTH, SiteScope
from ken_services.inventory.urls import (
    InvalidUrlError,
    build_scope,
    in_scope,
    normalize_netloc,
    normalize_url,
    path_key,
    try_normalize_url,
    url_netloc,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # case, default ports, empty path
        ("HTTP://Example.COM", "http://example.com/"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("https://example.com:8443/a", "https://example.com:8443/a"),
        ("http://example.com:443/a", "http://example.com:443/a"),
        ("https://example.com./a", "https://example.com/a"),
        # dot segments
        ("https://example.com/a/./b/../c", "https://example.com/a/c"),
        ("https://example.com/a/..", "https://example.com/"),
        ("https://example.com/../../x", "https://example.com/x"),
        ("https://example.com/a/.", "https://example.com/a/"),
        # percent-encoding
        ("https://example.com/%7euser", "https://example.com/~user"),
        ("https://example.com/a%2fb", "https://example.com/a%2Fb"),
        ("https://example.com/a b", "https://example.com/a%20b"),
        ("https://example.com/%e2%82%ac", "https://example.com/%E2%82%AC"),
        ("https://example.com/€", "https://example.com/%E2%82%AC"),
        ("https://example.com/100%", "https://example.com/100%25"),
        # IDNA
        ("https://Bücher.de/", "https://xn--bcher-kva.de/"),
        # fragments and tracking parameters removed; order and others kept
        ("https://example.com/a#section", "https://example.com/a"),
        (
            "https://example.com/?b=2&utm_source=x&a=1&UTM_Medium=y&gclid=z&fbclid=q",
            "https://example.com/?b=2&a=1",
        ),
        ("https://example.com/?utm_source=x", "https://example.com/"),
        # NOT changed: trailing slash, scheme, www
        ("https://example.com/a/", "https://example.com/a/"),
        ("http://www.example.com/a", "http://www.example.com/a"),
        # whitespace, tab and newline
        ("  https://example.com/a\n\tb  ", "https://example.com/ab"),
        # IPv6 literal
        ("https://[::1]:8080/a", "https://[::1]:8080/a"),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


def test_normalize_url_is_idempotent() -> None:
    once = normalize_url("HTTPS://Example.com:443/a/../b%7e?utm_source=x&q=1#f")
    assert normalize_url(once) == once


def test_relative_resolution_against_base() -> None:
    assert normalize_url("../x?q=1", base="https://example.com/a/b/c") == (
        "https://example.com/a/x?q=1"
    )
    assert normalize_url("//other.com/p", base="https://example.com/") == "https://other.com/p"
    assert normalize_url("?page=2", base="https://example.com/list") == (
        "https://example.com/list?page=2"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "ftp://example.com/file",
        "mailto:someone@example.com",
        "javascript:alert(1)",
        "data:text/html,hi",
        "https://user:secret@example.com/",
        "https://user@example.com/",
        "https://example.com:99999/",
        "https://example.com:notaport/",
        "https:///path-only",
        "/relative/without/base",
        "",
    ],
)
def test_rejected(raw: str) -> None:
    with pytest.raises(InvalidUrlError):
        normalize_url(raw)
    assert try_normalize_url(raw) is None


def test_rejection_message_never_echoes_the_url() -> None:
    with pytest.raises(InvalidUrlError) as excinfo:
        normalize_url("https://admin:hunter2@example.com/")
    assert "hunter2" not in str(excinfo.value)
    assert "example.com" not in str(excinfo.value)


def test_max_length() -> None:
    prefix = "https://example.com/"
    at_limit = prefix + "a" * (MAX_URL_LENGTH - len(prefix))
    assert normalize_url(at_limit) == at_limit
    with pytest.raises(InvalidUrlError):
        normalize_url(at_limit + "a")


def test_scope_basics() -> None:
    scope = build_scope("https://example.com/start")
    assert scope.allowed_hosts == frozenset({"example.com"})
    assert in_scope(normalize_url("https://example.com/a"), scope)
    # http and https on the same host are both in scope
    assert in_scope(normalize_url("http://example.com/a"), scope)
    # subdomains, including www, are NOT implied
    assert not in_scope(normalize_url("https://www.example.com/a"), scope)
    assert not in_scope(normalize_url("https://blog.example.com/a"), scope)
    assert not in_scope(normalize_url("https://example.com.evil.net/a"), scope)
    # a non-default port is a different network location
    assert not in_scope(normalize_url("https://example.com:8443/a"), scope)


def test_build_scope_extra_hosts_and_port_normalisation() -> None:
    scope = build_scope(
        "https://Example.com:443/", ["WWW.example.com:443", "cdn.example.com:8080", " "]
    )
    assert scope.allowed_hosts == frozenset(
        {"example.com", "www.example.com", "cdn.example.com:8080"}
    )
    assert in_scope(normalize_url("https://www.example.com/x"), scope)
    assert in_scope(normalize_url("https://cdn.example.com:8080/x"), scope)
    assert not in_scope(normalize_url("https://cdn.example.com/x"), scope)


def test_build_scope_with_port_in_base_url() -> None:
    scope = build_scope("http://localhost:8000/")
    assert scope.allowed_hosts == frozenset({"localhost:8000"})
    assert in_scope(normalize_url("http://localhost:8000/page"), scope)
    assert not in_scope(normalize_url("http://localhost/page"), scope)


def test_build_scope_rejects_invalid_base() -> None:
    with pytest.raises(InvalidUrlError):
        build_scope("https://user:pw@example.com/")
    with pytest.raises(InvalidUrlError):
        build_scope("not a url")


def test_netloc_helpers() -> None:
    assert normalize_netloc("Example.com:443") == "example.com"
    assert normalize_netloc("example.com:8080") == "example.com:8080"
    assert url_netloc("https://example.com:8080/a") == "example.com:8080"
    assert path_key("https://example.com/a/b?q=1") == "https://example.com/a/b"


def test_site_scope_model_validates_hosts() -> None:
    assert SiteScope(allowed_hosts="Example.com").allowed_hosts == frozenset({"example.com"})
    with pytest.raises(ValueError, match="bare host"):
        SiteScope(allowed_hosts=frozenset({"https://example.com"}))
    with pytest.raises(ValueError, match="bare host"):
        SiteScope(allowed_hosts=frozenset({"user@example.com"}))
