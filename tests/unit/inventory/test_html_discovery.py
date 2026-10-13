"""Shared HTML discovery (ken_services.inventory.html, frozen helper)."""

from __future__ import annotations

import pytest

from ken_services.inventory.html import PARSER, discover, discover_soup, make_soup


def test_parser_is_stdlib_html_parser() -> None:
    assert PARSER == "html.parser"


def test_discovers_canonical_base_robots_and_hrefs_in_order() -> None:
    html = """
    <html><head>
      <base href=" https://example.com/docs/ ">
      <link rel="stylesheet" href="/s.css">
      <link rel="Canonical" href=" /page ">
      <link rel="canonical" href="/second">
      <meta name="ROBOTS" content="NoIndex, nofollow">
      <meta name="robots" content="noarchive">
      <meta name="description" content="x">
    </head><body>
      <a href=" /one ">1</a><a>no href</a><a href="">empty</a>
      <p><a href="https://other.com/two">2</a></p>
      <a href="#frag">3</a>
    </body></html>
    """
    found = discover(html)
    assert found.hrefs == ("/one", "", "https://other.com/two", "#frag")
    assert found.base_href == "https://example.com/docs/"
    assert found.canonical_href == "/page"
    assert found.robots == frozenset({"noindex", "nofollow", "noarchive"})
    assert found.noindex
    assert found.nofollow


def test_canonical_as_one_of_several_rel_tokens() -> None:
    found = discover('<link rel="alternate canonical" href="/c">')
    assert found.canonical_href == "/c"


def test_robots_none_implies_noindex_and_nofollow() -> None:
    found = discover('<meta name="robots" content="none">')
    assert found.noindex
    assert found.nofollow


def test_empty_document() -> None:
    found = discover("")
    assert found.hrefs == ()
    assert found.base_href is None
    assert found.canonical_href is None
    assert found.robots == frozenset()
    assert not found.noindex
    assert not found.nofollow


def test_empty_canonical_and_base_are_none() -> None:
    found = discover('<base href="  "><link rel="canonical" href="">')
    assert found.base_href is None
    assert found.canonical_href is None


@pytest.mark.parametrize(
    "html",
    [
        "<html><body><p>unclosed <b>bold <i>italic",
        "</div></span><p>stray end tags</p></table>",
        "<table><tr><td><table><tr><td>nested<a href='/n'>n</a></table></td></tr></table>",
        "<a href='/x'<p>broken attribute",
        "<!-- unterminated comment",
        "<![if !supportLists]>conditional<![endif]>",
        "<\x00>&#xFFFFFFFF;<!DOCTYPE",
    ],
)
def test_malformed_html_never_raises(html: str) -> None:
    found = discover(html)
    assert isinstance(found.hrefs, tuple)


def test_discover_soup_matches_discover() -> None:
    html = '<link rel=canonical href="/c"><a href="/a">a</a>'
    assert discover_soup(make_soup(html)) == discover(html)
