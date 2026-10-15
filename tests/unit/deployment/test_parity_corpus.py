"""Locator robustness over the demo corpus (ADR-0014 section 8).

For every demo page, the content-block hashes are identical across five representations of
the same content:

* ``stored``  -- ``NormalizedPage.body_html`` (the normaliser's re-serialised content root);
* ``export``  -- the JSONL export fragment (``<main><article>...``), i.e. a CMS fragment;
* ``full``    -- a synthetic rendered page: doctype, head (title, style, script), header with
  nav, a comment, the fragment, an aside and a footer;
* ``no_main`` -- the fragment without its ``<main>`` (``<article>`` becomes the root);
* ``bare``    -- neither ``<main>`` nor ``<article>`` (the document is the root).

Plans made from the stored body then deploy onto every representation, and
``remove_links(insert_links(x)) == x`` byte for byte. Representations where parity does NOT
hold are pinned below (``test_documented_parity_breaks``), with the mutator refusing there.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable

import pytest

from ken_contracts.deployment import DeploymentIssueCode as Code
from ken_contracts.deployment import VerificationOutcome as Outcome
from ken_contracts.inventory import NormalizedPage
from ken_services.deployment.source_map import map_document
from ken_services.intelligence.taxonomy import load_taxonomy
from ken_services.inventory.hashing import sha256_hex
from ken_services.inventory.normalize import extract_blocks
from ken_services.recommendations.placement import (
    market_phrases,
    phrase_occurrences,
    span_is_safe,
)
from tests.unit.deployment._support import (
    EXCLUDED,
    MUTATOR,
    VERIFIER,
    bare,
    candidate_spans,
    codes,
    demo_fragments,
    demo_pages,
    full_document,
    insertion,
    removal,
    without_main,
)
from tests.unit.intelligence_evals.demo_corpus import TAXONOMY_PATH

REPRESENTATIONS: dict[str, Callable[[str], str]] = {
    "export": lambda fragment: fragment,
    "full": full_document,
    "no_main": without_main,
    "bare": bare,
}
ANCHORS_PER_PAGE = 8
"""Every anchor on the stored body; evenly sampled on the other four representations, to
keep the suite fast (all anchors everywhere: ~10k cases, ~1 min)."""
REFUSALS_ALLOWED = {
    Code.UNSAFE_PLACEMENT.value,
    Code.CONFLICTING_LINK.value,
    Code.ANCHOR_NOT_IN_SINGLE_TEXT_NODE.value,
}


def _signature(html: str) -> list[str]:
    return [sha256_hex(block.text) for block in extract_blocks(html)]


def _variants(url: str, stored: str) -> dict[str, str]:
    fragment = demo_fragments()[url]
    out = {"stored": stored}
    out.update({name: make(fragment) for name, make in REPRESENTATIONS.items()})
    return out


@pytest.mark.parametrize("page", demo_pages(), ids=lambda page: page.url)
def test_block_hashes_are_identical_across_representations(page: NormalizedPage) -> None:
    variants = _variants(page.url, page.body_html)
    expected = _signature(page.body_html)
    assert expected, "every demo page has content blocks"
    for name, html in variants.items():
        assert _signature(html) == expected, name
        assert map_document(html).sound, name


def test_every_demo_page_round_trips_on_every_representation() -> None:
    """Candidate anchors (word bigrams and taxonomy phrases) of every p/li block, deployed on
    every representation: the mutator applies EXACTLY the spans the engine's
    ``span_is_safe`` accepts on the stored body, and the public ``remove_links`` restores
    the input byte for byte."""
    taxonomy = load_taxonomy(TAXONOMY_PATH)
    phrases = market_phrases(taxonomy)
    excluded = {*EXCLUDED, "a"}
    outcomes: Counter[str] = Counter()
    for page in demo_pages():
        blocks = [b for b in extract_blocks(page.body_html) if b.kind in {"p", "li"}]
        anchors: list[tuple[int, str, int]] = []
        for index, block in enumerate(blocks):
            anchors.extend((index, phrase, occ) for phrase, occ in candidate_spans(block, limit=2))
            anchors.extend(
                (index, phrase, 0) for phrase in phrases if phrase_occurrences(phrase, block.text)
            )
        sampled = anchors[:: max(1, len(anchors) // ANCHORS_PER_PAGE)]
        for name, html in _variants(page.url, page.body_html).items():
            for n, (index, phrase, occurrence) in enumerate(
                anchors if name == "stored" else sampled
            ):
                block = blocks[index]
                item = insertion(
                    page.body_html, phrase, occurrence=occurrence, block=block, plan_id=n + 1
                )
                safe = span_is_safe(block, item.start, item.end, excluded_ancestors=excluded)
                result = MUTATOR.insert_links(html, [item], excluded_ancestors=EXCLUDED)
                assert result.ok is safe, (page.url, name, phrase, codes(result))
                if not result.ok:
                    assert set(codes(result)) <= REFUSALS_ALLOWED, (page.url, name, phrase)
                    assert result.html == html
                    outcomes[codes(result)[0]] += 1
                    continue
                outcomes["applied"] += 1
                undone = MUTATOR.remove_links(result.html, [removal(item)])
                assert undone.ok and undone.html == html, (page.url, name, phrase)
    assert outcomes["applied"] > 3000, outcomes
    assert outcomes[Code.UNSAFE_PLACEMENT.value] > 50, outcomes  # hub lists of links


def test_whole_page_batches_round_trip() -> None:
    """One safe anchor per block of a page, all in ONE request, on every representation:
    applied, verified (no failed check), and removed byte for byte."""
    pages = 0
    for page in demo_pages():
        items = []
        for index, block in enumerate(extract_blocks(page.body_html)):
            if block.kind not in {"p", "li"}:
                continue
            for phrase, occurrence in candidate_spans(block, limit=1):
                item = insertion(
                    page.body_html,
                    phrase,
                    occurrence=occurrence,
                    block=block,
                    plan_id=index + 1,
                    href=f"https://research.example.com/target-{index}",
                )
                single = MUTATOR.insert_links(page.body_html, [item], excluded_ancestors=EXCLUDED)
                if single.ok:
                    items.append(item)
        if len(items) < 2:
            continue
        pages += 1
        for html in _variants(page.url, page.body_html).values():
            result = MUTATOR.insert_links(html, items, excluded_ancestors=EXCLUDED)
            assert result.ok, (page.url, codes(result))
            assert len(result.applied) == len(items)
            checks = VERIFIER.verify_links(result.html, items)
            assert not [c for c in checks if c.outcome is Outcome.FAILED], page.url
            undone = MUTATOR.remove_links(result.html, [removal(i) for i in items])
            assert undone.ok and undone.html == html
    assert pages > 40


# --------------------------------------------------------------------------- #
# Where parity does not hold (pinned, and the mutator refuses)
# --------------------------------------------------------------------------- #
FRAGMENT_WITH_HEADER = (
    "<article><header><p>Battery storage briefing, October edition.</p></header>"
    "<p>Demand for battery storage is rising across India.</p></article>"
)


def test_documented_parity_breaks() -> None:
    stored = FRAGMENT_WITH_HEADER
    item = insertion(stored, "battery storage", contains="briefing")
    # 1. Without <article>/<main>, a <header> is no longer inside the content root's
    #    article and is stripped as boilerplate: its block disappears.
    no_root = stored.replace("<article>", "").replace("</article>", "")
    assert len(extract_blocks(no_root)) == len(extract_blocks(stored)) - 1
    result = MUTATOR.insert_links(no_root, [item], excluded_ancestors=EXCLUDED)
    assert codes(result) == [Code.BLOCK_NOT_FOUND.value] and result.html == no_root
    # 2. A rendered template whose <main> holds only part of the page selects a different
    #    content root: text outside that <main> is not content.
    template = f"<body>{stored}<main><p>Sidebar teaser.</p></main></body>"
    body_item = insertion(stored, "battery storage", contains="Demand")
    result = MUTATOR.insert_links(template, [body_item], excluded_ancestors=EXCLUDED)
    assert codes(result) == [Code.BLOCK_NOT_FOUND.value] and result.html == template
    # 3. The same block twice in a rendered page (e.g. a teaser repeating the lede) is found
    #    by hash in both places: the block_id hint decides, else ambiguous_block.
    from dataclasses import replace

    doubled = f"<main>{stored}<p>Demand for battery storage is rising across India.</p></main>"
    result = MUTATOR.insert_links(
        doubled, [replace(body_item, block_id_hint="b9")], excluded_ancestors=EXCLUDED
    )
    assert codes(result) == [Code.AMBIGUOUS_BLOCK.value]
