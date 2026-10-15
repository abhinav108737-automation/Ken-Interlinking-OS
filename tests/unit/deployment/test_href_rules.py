"""``href_rules``: the href we may write into a page (ADR-0014 section 3)."""

from __future__ import annotations

import html

import pytest

from ken_services.deployment.href_rules import (
    HrefProblem,
    escape_href,
    href_problem,
    hrefs_equivalent,
)


@pytest.mark.parametrize(
    "href",
    [
        "https://research.example.com/industry-reports/india-ev-charging-market",
        "http://research.example.com/",
        "https://research.example.com:8443/a?b=c&d=e#f",
        "HTTPS://Research.Example.com/x",
        "https://[2001:db8::1]/x",
        "https://research.example.com/a%20b",
    ],
)
def test_valid_hrefs(href: str) -> None:
    assert href_problem(href) is None


@pytest.mark.parametrize(
    ("href", "problem"),
    [
        ("", HrefProblem.EMPTY),
        ("https://x.example/" + "a" * 3000, HrefProblem.TOO_LONG),
        ("javascript:alert(1)", HrefProblem.SCHEME_NOT_ALLOWED),
        ("JAVASCRIPT:alert(1)", HrefProblem.SCHEME_NOT_ALLOWED),
        ("data:text/html;base64,PHNjcmlwdD4=", HrefProblem.SCHEME_NOT_ALLOWED),
        ("vbscript:msgbox", HrefProblem.SCHEME_NOT_ALLOWED),
        ("mailto:a@example.com", HrefProblem.SCHEME_NOT_ALLOWED),
        ("ftp://example.com/x", HrefProblem.SCHEME_NOT_ALLOWED),
        ("/relative", HrefProblem.SCHEME_NOT_ALLOWED),
        ("//research.example.com/x", HrefProblem.SCHEME_NOT_ALLOWED),
        ("java\tscript:alert(1)", HrefProblem.UNSAFE_CHARACTERS),
        (" https://research.example.com/", HrefProblem.UNSAFE_CHARACTERS),
        ("https://research.example.com/a b", HrefProblem.UNSAFE_CHARACTERS),
        ("https://research.example.com/\xe9", HrefProblem.UNSAFE_CHARACTERS),
        ("https://research.example.com/\x00", HrefProblem.UNSAFE_CHARACTERS),
        ("https:\\\\evil.example.com/", HrefProblem.UNSAFE_CHARACTERS),
        ("https://user:pw@research.example.com/", HrefProblem.CREDENTIALS),
        ("https://user@research.example.com/", HrefProblem.CREDENTIALS),
        ("https://research.example.com:99999/", HrefProblem.INVALID_PORT),
        ("https://research.example.com:abc/", HrefProblem.INVALID_PORT),
        ("https://research.example.com:0/", HrefProblem.INVALID_PORT),
        ("https:///path-only", HrefProblem.NO_HOST),
        ("https://[::1/x", HrefProblem.UNPARSEABLE),
    ],
)
def test_invalid_hrefs(href: str, problem: HrefProblem) -> None:
    assert href_problem(href) is problem


def test_allowed_hosts() -> None:
    hosts = ["research.example.com", "WWW.Research.Example.com."]
    assert href_problem("https://research.example.com/x", allowed_hosts=hosts) is None
    assert href_problem("https://www.research.example.com/x", allowed_hosts=hosts) is None
    assert (
        href_problem("https://evil.example.org/x", allowed_hosts=hosts)
        is HrefProblem.HOST_NOT_ALLOWED
    )
    assert (
        href_problem("https://research.example.com.evil.org/", allowed_hosts=hosts)
        is HrefProblem.HOST_NOT_ALLOWED
    )
    assert href_problem("https://x.example/", allowed_hosts=[]) is HrefProblem.HOST_NOT_ALLOWED


@pytest.mark.parametrize("href", ['https://x.example/?q="a"&b=<c>', "https://x.example/'x'"])
def test_escape_href_round_trips_through_attribute_unescaping(href: str) -> None:
    escaped = escape_href(href)
    assert not any(char in escaped for char in "\"'<>")
    assert html.unescape(escaped) == href


def test_hrefs_equivalent() -> None:
    a = "https://research.example.com/x?a=1#top"
    assert hrefs_equivalent(a, a)
    assert hrefs_equivalent(" " + a, a)
    assert hrefs_equivalent("HTTPS://Research.Example.com:443/x?a=1#top", a)
    assert not hrefs_equivalent("https://research.example.com/x?a=1#bottom", a)
    assert not hrefs_equivalent("https://research.example.com/y", a)
    assert not hrefs_equivalent(None, a)
    assert not hrefs_equivalent(a, None)
    assert not hrefs_equivalent("javascript:x", "javascript:y")
