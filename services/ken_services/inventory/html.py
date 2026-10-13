"""Shared HTML discovery. FROZEN (orchestrator-owned).

The crawler (which links to follow) and the normaliser (canonical, robots,
base URL) must read these facts the SAME way, or a page could be crawled under
one canonical and stored under another. Both import them from here.

Parser: BeautifulSoup with Python's built-in ``html.parser`` backend -- no lxml
or html5lib, so there is no native code and nothing extra to licence-check for
a future desktop build. ``html.parser`` tolerates malformed markup (unclosed
tags, stray end tags) without raising.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from bs4 import BeautifulSoup, Tag

PARSER: Final = "html.parser"


def make_soup(html: str) -> BeautifulSoup:
    """Parse with the one parser the inventory uses everywhere."""
    return BeautifulSoup(html, PARSER)


def _attr(tag: Tag, name: str) -> str | None:
    value = tag.get(name)
    if value is None:
        return None
    if isinstance(value, list):  # multi-valued attributes (class, rel)
        return " ".join(str(part) for part in value)
    return str(value)


def _tokens(value: str | None) -> frozenset[str]:
    if not value:
        return frozenset()
    return frozenset(token.strip().lower() for token in value.replace(",", " ").split() if token)


@dataclass(frozen=True, slots=True)
class HtmlDiscovery:
    """Document-level facts plus every ``<a href>``, in document order."""

    hrefs: tuple[str, ...]
    """Raw, whitespace-trimmed ``href`` values of every anchor in the document."""
    base_href: str | None
    """First ``<base href>``, raw. URLs in the document resolve against it."""
    canonical_href: str | None
    """First ``<link rel="canonical" href>``, raw."""
    robots: frozenset[str]
    """Lower-cased tokens of every ``<meta name="robots">`` (e.g. noindex, nofollow)."""

    @property
    def nofollow(self) -> bool:
        return "nofollow" in self.robots or "none" in self.robots

    @property
    def noindex(self) -> bool:
        return "noindex" in self.robots or "none" in self.robots


def discover_soup(soup: BeautifulSoup) -> HtmlDiscovery:
    """:func:`discover` over an already-parsed document."""
    hrefs: list[str] = []
    for anchor in soup.find_all("a", href=True):
        if isinstance(anchor, Tag):
            href = _attr(anchor, "href")
            if href is not None:
                hrefs.append(href.strip())

    base_href: str | None = None
    base = soup.find("base", href=True)
    if isinstance(base, Tag):
        base_href = (_attr(base, "href") or "").strip() or None

    canonical_href: str | None = None
    for link in soup.find_all("link", href=True):
        if isinstance(link, Tag) and "canonical" in _tokens(_attr(link, "rel")):
            canonical_href = (_attr(link, "href") or "").strip() or None
            break

    robots: set[str] = set()
    for meta in soup.find_all("meta"):
        if isinstance(meta, Tag) and (_attr(meta, "name") or "").strip().lower() == "robots":
            robots |= _tokens(_attr(meta, "content"))

    return HtmlDiscovery(
        hrefs=tuple(hrefs),
        base_href=base_href,
        canonical_href=canonical_href,
        robots=frozenset(robots),
    )


def discover(html: str) -> HtmlDiscovery:
    """Parse ``html`` and return its discovery facts. Never raises on malformed markup."""
    return discover_soup(make_soup(html))
