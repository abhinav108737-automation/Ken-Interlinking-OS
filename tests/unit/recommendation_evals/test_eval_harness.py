"""The eval harness itself: must-not checks fire on bad plans, metrics follow the README.

Synthetic records only (no database), so a silently broken gate cannot pass the evals.
"""

from __future__ import annotations

import dataclasses
import hashlib

import pytest

from ken_contracts.recommendations import RecommendationPolicy
from ken_services.inventory.normalize import extract_blocks
from tests.unit.intelligence_evals import demo_corpus as dc
from tests.unit.recommendation_evals.judgments import (
    PageState,
    PlanRecord,
    judgments,
    must_not_violations,
    quality_metrics,
)

POLICY = RecommendationPolicy.model_validate(
    {
        "policy": "recommendation",
        "version": 1,
        "status": "test",
        "source": "test",
        "ranking_policy": "policies/ranking/ranking_weights.v1.yaml",
        "intent_compatibility": {},
        "page_type_targets": {},
    }
)
BODY = (
    "<main><article><h1>Grid notes</h1>"
    "<p>Operators pair chargers with battery storage in India today.</p>"
    '<p>See <a href="https://research.example.com/x">rooftop solar</a> and rooftop solar again.</p>'
    "<h2>battery storage outlook</h2>"
    "</article></main>"
)
BLOCKS = extract_blocks(BODY)
P1 = BLOCKS[1]  # free paragraph
P2 = BLOCKS[2]  # paragraph that already holds a link
SRC, TGT, OTHER = "s" * 8, "t" * 8, "o" * 8


def _page(page_id: str, url: str, **kw: object) -> PageState:
    base: dict[str, object] = {
        "page_id": page_id,
        "canonical_url": url,
        "current_version": 1,
        "body_html": BODY,
        "word_count": 300,
        "gone": False,
        "protected": False,
        "market_id": None,
        "linked_page_ids": frozenset(),
        "internal_body_links": 1,
        "links_by_block": {P2.block_id: ("rooftop solar",)},
    }
    base.update(kw)
    return PageState(**base)  # type: ignore[arg-type]


PAGES = {
    SRC: _page(
        SRC, "https://research.example.com/industry-reports/india-ev-charging-infrastructure-market"
    ),
    TGT: _page(
        TGT, "https://research.example.com/industry-reports/india-battery-energy-storage-market"
    ),
    OTHER: _page(OTHER, "https://research.example.com/industry-reports/india-cold-chain-market"),
}


def _plan(**kw: object) -> PlanRecord:
    start = P1.text.index("battery storage")
    base: dict[str, object] = {
        "id": "p1",
        "run_id": "r1",
        "status": "proposed",
        "source_page_id": SRC,
        "source_url": PAGES[SRC].canonical_url,
        "source_page_version": 1,
        "target_page_id": TGT,
        "target_url": PAGES[TGT].canonical_url,
        "target_page_version": 1,
        "anchor_text": "battery storage",
        "anchor_source": "target_topic",
        "block_id": P1.block_id,
        "block_kind": "p",
        "occurrence_index": 0,
        "start": start,
        "end": start + len("battery storage"),
        "context_hash": hashlib.sha256(P1.text.encode()).hexdigest(),
        "total_score": 0.6,
        "confidence": 0.6,
        "calibration_version": None,
        "relevance": 0.6,
        "factors": {"placement_quality": 1.0},
        "components": {"graph": 0.15},
        "relationship_type": None,
        "channels": ("taxonomy",),
    }
    base.update(kw)
    return PlanRecord(**base)  # type: ignore[arg-type]


def _violations(*plans: PlanRecord, pages: dict[str, PageState] | None = None) -> set[str]:
    return set(
        must_not_violations(list(plans), pages or PAGES, policy=POLICY, taxonomy=dc.taxonomy())
    )


def test_a_clean_plan_passes_every_gate() -> None:
    assert _violations(_plan()) == set()


def _at(block_index: int, phrase: str, nth: int = 0) -> dict[str, object]:
    block = BLOCKS[block_index]
    starts = [i for i in range(len(block.text)) if block.text.startswith(phrase, i)]
    return {
        "anchor_text": phrase,
        "block_id": block.block_id,
        "block_kind": block.kind,
        "start": starts[nth],
        "end": starts[nth] + len(phrase),
        "occurrence_index": nth,
        "context_hash": hashlib.sha256(block.text.encode()).hexdigest(),
    }


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        ({"target_page_id": SRC, "target_url": PAGES[SRC].canonical_url}, "MN-self"),
        ({"anchor_text": "battery storagX"}, "MN-verbatim"),
        ({"occurrence_index": 1}, "MN-verbatim"),
        ({"context_hash": "0" * 64}, "MN-verbatim"),
        (_at(1, "India"), "MN-geo-anchor"),
        ({**_at(2, "rooftop solar", 1)}, "MN-link-overlap"),
        ({**_at(2, "rooftop solar", 0)}, "MN-excluded-ancestor"),
        (_at(3, "battery storage"), "MN-block-kind"),
        ({"calibration_version": "cal-1"}, "MN-raw-confidence"),
        ({"total_score": 0.2, "confidence": 0.2, "relevance": 0.2}, "MN-thresholds"),
        ({"total_score": 0.5, "confidence": 0.5}, "MN-thresholds"),
        ({"source_page_version": 2}, "MN-current-version"),
        (
            {"target_url": "https://research.example.com/privacy"},
            "MN-target-ineligible",
        ),
    ],
)
def test_each_gate_fires(change: dict[str, object], rule: str) -> None:
    assert rule in _violations(_plan(**change))


def test_page_state_gates_fire() -> None:
    pages = dict(PAGES)
    pages[TGT] = dataclasses.replace(PAGES[TGT], protected=True)
    assert "MN-protected" in _violations(_plan(), pages=pages)
    pages[TGT] = dataclasses.replace(PAGES[TGT], gone=True)
    assert "MN-gone" in _violations(_plan(), pages=pages)
    pages[SRC] = dataclasses.replace(PAGES[SRC], linked_page_ids=frozenset({TGT}))
    assert "MN-already-linked" in _violations(_plan(), pages=pages)
    pages = dict(PAGES)
    pages[SRC] = dataclasses.replace(PAGES[SRC], word_count=60, internal_body_links=1)
    assert "MN-density" in _violations(_plan(), pages=pages)


def test_caps_and_uniqueness_gates_fire() -> None:
    twin = _plan(id="p2")
    assert {"MN-open-pair", "MN-open-span", "MN-link-overlap"} <= _violations(_plan(), twin)
    many = [
        _plan(
            id=f"p{i}",
            target_page_id=f"x{i}",
            target_url=f"u{i}",
            start=0,
            end=9,
            anchor_text=P1.text[0:9],
            context_hash=f"{i:064x}",
        )
        for i in range(4)
    ]
    pages = dict(PAGES) | {f"x{i}": _page(f"x{i}", f"u{i}") for i in range(4)}
    assert "MN-per-source" in _violations(*many, pages=pages)


def test_quality_metrics_follow_the_fixture_conventions() -> None:
    source = "https://research.example.com/industry-reports/india-ev-charging-infrastructure-market"
    entry = judgments()["sources"][source]
    good = [e["target"] for e in entry["good"]]
    bad = next(e["target"] for e in entry["bad"] if e["kind"] == "shared_word")
    plans = [
        _plan(id="a", source_url=source, target_url=good[0], target_page_id="g0"),
        _plan(id="b", source_url=source, target_url=good[1], target_page_id="g1"),
        _plan(id="c", source_url=source, target_url=bad, target_page_id="b0"),
    ]
    metrics = quality_metrics(plans, policy=POLICY, taxonomy=dc.taxonomy(), target_markets={})
    row = metrics["per_source"][source]
    assert [p["label"] for p in row["plans"]] == ["good", "good", "bad"]
    assert metrics["precision_at_3_macro"] == round(2 / 3, 4)
    assert metrics["bad_rate"] == round(1 / 3, 4)
    # Recall over min(|GOOD|, cap) for this one source; every other judged source has
    # no plans, so the macro mean includes their zeros.
    with_good = sum(1 for e in judgments()["sources"].values() if e["good"])
    assert metrics["recall_good_macro"] == round((2 / min(len(good), 3)) / with_good, 4)
