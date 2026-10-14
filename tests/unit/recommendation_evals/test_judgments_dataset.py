"""Integrity of the Phase 3 recommendation judgments (no database, no engine).

The judgments are editorial and spec-derived; these tests only make sure they are
internally consistent with the corpus, the Phase 2 goldens and labels, and the ADR-0011
placement rules, so a metric computed against them means what the README says.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ken_contracts.inventory import NormalizedPage
from ken_services.intelligence.analyze import analyze_page
from ken_services.intelligence.eligibility import compute_eligibility
from ken_services.inventory.normalize import ContentBlock, extract_blocks
from tests.unit.intelligence_evals import demo_corpus as dc
from tests.unit.recommendation_evals.judgments import (
    corpus_with_safety_page,
    goldens,
    judgments,
    market_phrases,
    must_not_pages,
    source_judgments,
)

SOURCES = judgments()["sources"]
EXPECTED = judgments()["expected_anchors"]
KINDS = {"geography_only", "shared_word", "intent_mismatch", "unrelated_market", "topic_mismatch",
         "ineligible"}  # fmt: skip


def _occurrences(phrase: str, text: str) -> list[int]:
    pattern = re.compile(r"(?<![\w-])" + re.escape(phrase) + r"(?![\w-])", re.IGNORECASE)
    return [m.start() for m in pattern.finditer(text)]


def _free_blocks(page: NormalizedPage) -> list[ContentBlock]:
    """Blocks a plan may use under policy v1: p/li and no existing link (max 1 per block)."""
    linked = {link.block_id for link in page.links}
    return [
        b
        for b in extract_blocks(page.body_html)
        if b.kind in ("p", "li") and b.block_id not in linked
    ]


def _linked_targets(page: NormalizedPage) -> set[str]:
    canonical = {p.url: p.canonical_url for p in dc.load_pages()}
    return {
        canonical.get(link.resolved_url, link.resolved_url)
        for link in page.links
        if link.link_type.value == "internal" and link.resolved_url is not None
    }


def test_inventory_meets_the_task_minimums() -> None:
    assert len(SOURCES) >= 25
    assert len(EXPECTED) >= 15
    assert sum(len(e["good"]) for e in SOURCES.values()) >= 50
    kinds = {b["kind"] for e in SOURCES.values() for b in e["bad"]}
    assert {"geography_only", "shared_word", "intent_mismatch"} <= kinds <= KINDS


def test_sources_are_eligible_and_every_url_is_in_the_corpus() -> None:
    urls = set(dc.pages_by_url())
    for source, entry in SOURCES.items():
        assert goldens()["pages"][source]["eligibility"]["as_source"], source
        for bucket in ("good", "acceptable", "bad"):
            for item in entry[bucket]:
                assert item["target"] in urls, item["target"]
                assert item["target"] != source


def test_buckets_are_disjoint_and_consistent_with_phase2_labels() -> None:
    rel = dc.load_relatedness()
    never_target = must_not_pages("MN-target-ineligible")
    for source, verdict in source_judgments().items():
        assert not verdict.good & verdict.acceptable, source
        assert not (verdict.good | verdict.acceptable) & verdict.bad, source
        assert not (verdict.good | verdict.acceptable) & never_target, source
        for target in verdict.good | verdict.acceptable:
            assert rel.labels.get(frozenset((source, target))) != "unrelated", (source, target)
        for target in verdict.bad:
            assert rel.labels.get(frozenset((source, target))) != "strong", (source, target)


@pytest.mark.parametrize("source", sorted(SOURCES))
def test_good_targets_have_an_unlinked_natural_mention(source: str) -> None:
    page = dc.pages_by_url()[source]
    linked = _linked_targets(page)
    text = " ".join(b.text for b in _free_blocks(page))
    for item in SOURCES[source]["good"]:
        assert item["target"] not in linked, item
        assert _occurrences(item["mention"], text), item
    for item in SOURCES[source]["acceptable"]:
        assert item["target"] not in linked, item


@pytest.mark.parametrize(
    "item", EXPECTED, ids=lambda i: f"{i['source'][-30:]}->{i['target'][-30:]}"
)
def test_expected_anchor_is_the_only_market_phrase(item: dict[str, str]) -> None:
    page = dc.pages_by_url()[item["source"]]
    market = dc.analyses()[item["target"]].market_id
    hits = [
        b.text[s : s + len(phrase)]
        for b in _free_blocks(page)
        for phrase in market_phrases(dc.taxonomy(), market)
        for s in _occurrences(phrase, b.text)
    ]
    assert hits == [item["anchor"]], hits
    assert item["judgment"] in ("good", "acceptable")
    assert item["target"] not in _linked_targets(page)


def test_must_not_page_lists_match_the_eligibility_goldens() -> None:
    pages = goldens()["pages"]
    assert must_not_pages("MN-source-ineligible") == {
        u for u, g in pages.items() if not g["eligibility"]["as_source"]
    }
    assert must_not_pages("MN-target-ineligible") == {
        u for u, g in pages.items() if not g["eligibility"]["as_target"]
    }
    # The special cases the task names are all covered.
    never_source = must_not_pages("MN-source-ineligible")
    for path in ("/", "/industries/energy", "/about", "/privacy", "/contact",
                 "/industry-reports/saudi-arabia-medical-devices-market",
                 "/industry-reports/vietnam-ev-charging-market", "/insights/webinar-recap-october",
                 "/industry-reports/india-electric-two-wheeler-market-2025-edition"):  # fmt: skip
        assert f"https://research.example.com{path}" in never_source, path


def test_placement_safety_page_is_an_eligible_source_with_two_control_blocks(
    tmp_path: Path,
) -> None:
    safety = judgments()["placement_safety"]
    corpus = corpus_with_safety_page(tmp_path / "corpus.jsonl")
    header, *records = corpus.read_text("utf-8").splitlines()
    assert json.loads(records[-1]) == safety["record"]
    only = tmp_path / "safety.jsonl"
    only.write_text(f"{header}\n{records[-1]}\n", "utf-8")
    import asyncio

    (page,) = asyncio.run(dc._read_corpus(only))
    analysis = analyze_page(dc.analysis_input(page), taxonomy=dc.taxonomy())
    eligibility = compute_eligibility(dc.eligibility_state(page, analysis), taxonomy=dc.taxonomy())
    assert eligibility.as_source, eligibility
    texts = [b.text for b in extract_blocks(page.body_html)]
    for allowed in safety["allowed_block_texts"]:
        assert texts.count(allowed) == 1, allowed
