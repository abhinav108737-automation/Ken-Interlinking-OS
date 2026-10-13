"""Regression tests for the Phase 1 QA/security findings (M1-M3, L2, L4).

Each test pins a fix: hostile content must stay linear, NUL must never reach
PostgreSQL, robots.txt matching must be linear and bounded, a robots.txt 429
means "slow down" (disallow), and responses are requested uncompressed so the
byte cap bounds memory. Timing bounds are generous (10x+ headroom) so the
suite stays stable on a slow machine while still catching an O(n^2) regression.
"""

from __future__ import annotations

import random
import re
import time
from datetime import UTC, datetime

import pytest

from ken_contracts.inventory import IssueCode, SourceAssertions, SourceDocument, SourceType
from ken_services.inventory.normalize import (
    ANCHOR_TEXT_MAX,
    MAX_LINKS_PER_PAGE,
    normalize_document,
)
from ken_services.inventory.robots import (
    MAX_PATTERN_LENGTH,
    MAX_RULES,
    RobotsRule,
    parse_robots,
)
from ken_services.inventory.urls import build_scope
from tests.unit.inventory.test_public_web_source import MockSite, crawl, docs, html, issues, u

SCOPE = build_scope("https://site.test/")


def _doc(html_text: str, **assertions: object) -> SourceDocument:
    return SourceDocument(
        source_type=SourceType.PUBLIC_WEB,
        source_id="https://site.test/",
        requested_url="https://site.test/",
        final_url="https://site.test/",
        http_status=200,
        fetched_at=datetime(2026, 10, 13, tzinfo=UTC),
        html=html_text,
        assertions=SourceAssertions(**assertions),
    )


def _timed(html_text: str) -> float:
    started = time.perf_counter()
    normalize_document(_doc(html_text), scope=SCOPE)
    return time.perf_counter() - started


# --------------------------------------------------------------------------- #
# M1: hostile markup stays linear
# --------------------------------------------------------------------------- #
def test_nested_anchors_are_linear_not_quadratic() -> None:
    """html.parser never auto-closes <a>: 4,000 nested anchors took 3.65 s before the fix."""
    small = _timed("<main>" + "<a href='/x'>t" * 2_000 + "</main>")
    large = _timed("<main>" + "<a href='/x'>t" * 8_000 + "</main>")
    assert large < 15.0
    # Linear: 4x the input costs ~4x; quadratic would cost ~16x.
    assert large < small * 9 + 0.5


def test_many_links_in_one_huge_block_hash_the_block_once() -> None:
    """2.5 MB of text and 20k links in one <p> took 21.5 s before the fix."""
    links = "".join(f"<a href='/l{i}'>a{i}</a>" for i in range(20_000))
    assert _timed("<main><p>" + "word " * 500_000 + links + "</p></main>") < 10.0


def test_links_per_page_are_capped_and_flagged() -> None:
    links = "".join(f"<a href='/l{i}'>a</a> " for i in range(MAX_LINKS_PER_PAGE + 5))
    page = normalize_document(_doc(f"<main><p>{links}</p></main>"), scope=SCOPE)
    assert len(page.links) == MAX_LINKS_PER_PAGE
    assert page.metadata["links_truncated"] == str(MAX_LINKS_PER_PAGE + 5)


def test_anchor_text_is_bounded() -> None:
    page = normalize_document(
        _doc("<main><p><a href='/x'>" + "y" * (ANCHOR_TEXT_MAX * 3) + "</a></p></main>"),
        scope=SCOPE,
    )
    assert len(page.links[0].anchor_text) == ANCHOR_TEXT_MAX


# --------------------------------------------------------------------------- #
# M2: NUL never reaches PostgreSQL
# --------------------------------------------------------------------------- #
def test_nul_is_replaced_everywhere_it_could_be_stored() -> None:
    page = normalize_document(
        _doc(
            "<html lang='en'><head><title>T\x00</title></head><main><p>a\x00b "
            "<a href='/x\x00y'>c\x00</a></p></main></html>",
            title="asserted\x00",
            metadata={"k\x00": "v\x00"},
            external_id="cms\x00-1",
        ),
        scope=SCOPE,
    )
    dumped = page.model_dump_json()
    assert "\\u0000" not in dumped
    assert "\x00" not in dumped
    assert page.title == "asserted�"
    assert page.body_text == "a�b c�"


# --------------------------------------------------------------------------- #
# M3: robots.txt matching is linear and bounded
# --------------------------------------------------------------------------- #
def _reference(pattern: str, path: str) -> bool:
    """Independent oracle: robots wildcards as an anchored regex."""
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.match(regex + ("$" if anchored else ""), path, re.DOTALL) is not None


@pytest.mark.parametrize("seed", range(5))
def test_matcher_agrees_with_a_regex_oracle(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311 -- seeded, deterministic test data
    alphabet = "/ab*"
    for _ in range(2_000):
        pattern = "/" + "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 8)))
        if rng.random() < 0.3:
            pattern += "$"
        path = "/" + "".join(rng.choice("/ab") for _ in range(rng.randint(0, 10)))
        rule = RobotsRule(allow=False, pattern=pattern)
        assert rule.matches(path) is _reference(pattern, path), (pattern, path)


def test_hostile_pattern_is_fast() -> None:
    """QA M3: ~0.1 s per rule before the fix for this pattern and path."""
    rules = parse_robots(
        "User-agent: *\n" + "".join(f"Disallow: /*{'a' * 1000}b{i}\n" for i in range(200))
    ).policy_for("KenInterlinkBot/0.1")
    started = time.perf_counter()
    assert rules.can_fetch("/" + "a" * 2000)
    assert time.perf_counter() - started < 1.0


def test_overlong_patterns_and_excess_rules_are_ignored() -> None:
    long_rule = "Disallow: /" + "x" * MAX_PATTERN_LENGTH + "\n"
    many = "".join(f"Disallow: /r{i}\n" for i in range(MAX_RULES + 10))
    policy = parse_robots("User-agent: *\n" + long_rule + many).policy_for("Bot")
    assert len(policy.rules) == MAX_RULES
    assert policy.can_fetch("/" + "x" * MAX_PATTERN_LENGTH)  # the long rule was dropped
    assert not policy.can_fetch("/r0")


# --------------------------------------------------------------------------- #
# L4 / L2: robots 429; uncompressed responses
# --------------------------------------------------------------------------- #
async def test_robots_429_means_slow_down_so_nothing_is_fetched() -> None:
    site = MockSite(
        {u("/robots.txt"): (429, {"content-type": "text/plain"}, "busy"), u("/"): html("/a")}
    )
    records, _ = await crawl(site)
    assert site.urls() == [u("/robots.txt")]
    assert issues(records, IssueCode.ROBOTS_UNAVAILABLE)
    assert docs(records) == []


async def test_pages_are_requested_uncompressed() -> None:
    site = MockSite({u("/robots.txt"): (404, {}, ""), u("/"): html()})
    await crawl(site)
    assert site.requests
    assert all(r.headers["accept-encoding"] == "identity" for r in site.requests)
