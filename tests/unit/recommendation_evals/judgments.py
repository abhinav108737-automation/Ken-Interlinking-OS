"""Pure helpers for the Phase 3 recommendation evals (ADR-0011).

Used by ``tests/integration/test_recommendation_evals.py`` and
``evals/recommendations/benchmark.py``. Everything here is a pure function over plain
records: no database, no network, no model. ``db_flow`` reads the records from the test
database; this module scores them against the spec-derived judgments in
``tests/fixtures/demo/recommendation_judgments.json`` and checks the must-not rules.

Nothing here proposes, promotes or changes a policy: it is an offline eval
(RUN_OFFLINE_EVAL, ALLOW) that reads fixtures and returns numbers.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, Final

from ken_contracts.intelligence import TaxonomyPolicy
from ken_contracts.recommendations import OPEN_STATUSES, RecommendationPolicy
from ken_services.inventory.normalize import ContentBlock, extract_blocks

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
DEMO_DIR: Final = REPO_ROOT / "tests" / "fixtures" / "demo"
CORPUS_PATH: Final = DEMO_DIR / "research_site.jsonl"
JUDGMENTS_PATH: Final = DEMO_DIR / "recommendation_judgments.json"
STALE_CASES_PATH: Final = DEMO_DIR / "stale_context_cases.json"
GOLDENS_PATH: Final = DEMO_DIR / "analysis_expected.json"
OPEN: Final = frozenset(status.value for status in OPEN_STATUSES)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text("utf-8"))
    return data


@cache
def judgments() -> dict[str, Any]:
    return _load(JUDGMENTS_PATH)


@cache
def stale_cases() -> dict[str, Any]:
    return _load(STALE_CASES_PATH)


@cache
def goldens() -> dict[str, Any]:
    return _load(GOLDENS_PATH)


@dataclass(frozen=True, slots=True)
class SourceJudgment:
    good: frozenset[str]
    acceptable: frozenset[str]
    bad: frozenset[str]
    bad_kind: Mapping[str, str]

    def label(self, target: str) -> str:
        if target in self.good:
            return "good"
        if target in self.acceptable:
            return "acceptable"
        if target in self.bad:
            return "bad"
        return "unjudged"


def source_judgments() -> dict[str, SourceJudgment]:
    out: dict[str, SourceJudgment] = {}
    for source, entry in judgments()["sources"].items():
        out[source] = SourceJudgment(
            good=frozenset(e["target"] for e in entry["good"]),
            acceptable=frozenset(e["target"] for e in entry["acceptable"]),
            bad=frozenset(e["target"] for e in entry["bad"]),
            bad_kind={e["target"]: e["kind"] for e in entry["bad"]},
        )
    return out


def must_not_pages(rule_id: str) -> frozenset[str]:
    for rule in judgments()["must_not"]["rules"]:
        if rule["id"] == rule_id:
            return frozenset(rule.get("pages", ()))
    raise KeyError(rule_id)


def corpus_with_safety_page(destination: Path) -> Path:
    """The demo JSONL plus the placement-safety record (written to ``destination``)."""
    lines = CORPUS_PATH.read_text("utf-8").splitlines()
    record = judgments()["placement_safety"]["record"]
    destination.write_text("\n".join([*lines, json.dumps(record)]) + "\n", "utf-8")
    return destination


# --------------------------------------------------------------------------- #
# Records read back from the database (db_flow builds them)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class PlanRecord:
    id: str
    run_id: str | None
    status: str
    source_page_id: str
    source_url: str
    source_page_version: int
    target_page_id: str
    target_url: str
    target_page_version: int
    anchor_text: str
    anchor_source: str
    block_id: str
    block_kind: str
    occurrence_index: int
    start: int
    end: int
    context_hash: str
    total_score: float
    confidence: float
    calibration_version: str | None
    relevance: float
    factors: Mapping[str, float]
    components: Mapping[str, float]
    """Component name -> contribution (value x weight)."""
    relationship_type: str | None
    channels: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PageState:
    page_id: str
    canonical_url: str
    current_version: int
    body_html: str
    word_count: int
    gone: bool
    protected: bool
    market_id: str | None
    linked_page_ids: frozenset[str]
    """Pages the CURRENT version links to (internal links resolved through known_urls)."""
    internal_body_links: int
    links_by_block: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    """block_id -> anchor texts of every link (any type) in that block, current version."""

    def blocks(self) -> tuple[ContentBlock, ...]:
        return _blocks(self.body_html)


@cache
def _blocks(body_html: str) -> tuple[ContentBlock, ...]:
    return extract_blocks(body_html)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def locate_block(plan: PlanRecord, page: PageState) -> ContentBlock | None:
    """The block a plan points at: by context hash, the block_id hint breaking ties."""
    matches = [b for b in page.blocks() if sha256_hex(b.text) == plan.context_hash]
    matches.sort(key=lambda b: b.block_id != plan.block_id)
    return matches[0] if matches else None


def _occurrences(phrase: str, text: str) -> list[int]:
    pattern = re.compile(r"(?<![\w-])" + re.escape(phrase) + r"(?![\w-])", re.IGNORECASE)
    return [m.start() for m in pattern.finditer(text)]


def _spans_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def geography_phrases(taxonomy: TaxonomyPolicy) -> frozenset[str]:
    return frozenset(
        phrase.lower() for geo in taxonomy.geographies for phrase in (geo.label, *geo.aliases)
    )


def market_phrases(taxonomy: TaxonomyPolicy, market_id: str | None) -> frozenset[str]:
    for market in taxonomy.markets:
        if market.id == market_id:
            return frozenset(p.lower() for p in (market.label, *market.aliases))
    return frozenset()


# --------------------------------------------------------------------------- #
# Must-not rules (hard gates, zero tolerance)
# --------------------------------------------------------------------------- #
def must_not_violations(
    plans: Sequence[PlanRecord],
    pages: Mapping[str, PageState],
    *,
    policy: RecommendationPolicy,
    taxonomy: TaxonomyPolicy,
) -> dict[str, list[str]]:
    """Rule id -> human-readable violations. Every list must be empty."""
    found: dict[str, list[str]] = defaultdict(list)
    never_source = must_not_pages("MN-source-ineligible")
    never_target = must_not_pages("MN-target-ineligible")
    geo = geography_phrases(taxonomy)
    place = policy.placement
    select = policy.selection
    forbidden = {a.lower() for a in place.forbidden_anchors}
    excluded = frozenset(place.excluded_ancestors)

    def bad(rule: str, plan: PlanRecord, detail: str) -> None:
        found[rule].append(
            f"{plan.source_url} -> {plan.target_url} [{plan.anchor_text!r}]: {detail}"
        )

    open_plans = [p for p in plans if p.status in OPEN]
    planned_spans: dict[tuple[str, str], list[tuple[int, int, str]]] = defaultdict(list)
    for plan in open_plans:
        planned_spans[(plan.source_page_id, plan.context_hash)].append(
            (plan.start, plan.end, plan.id)
        )

    for plan in plans:
        src, tgt = pages.get(plan.source_page_id), pages.get(plan.target_page_id)
        if src is None or tgt is None:
            bad("MN-verbatim", plan, "source or target page unknown")
            continue
        if plan.source_url in never_source or src.canonical_url in never_source:
            bad("MN-source-ineligible", plan, "source is not linkable as a source")
        if plan.target_url in never_target or tgt.canonical_url in never_target:
            bad("MN-target-ineligible", plan, "target is not linkable as a target")
        if src.protected or tgt.protected:
            bad("MN-protected", plan, "protected source or target")
        if src.gone or tgt.gone:
            bad("MN-gone", plan, "gone source or target")
        if plan.source_page_id == plan.target_page_id or src.canonical_url == tgt.canonical_url:
            bad("MN-self", plan, "self-link")
        if plan.source_page_version != src.current_version:
            bad("MN-current-version", plan, "source version is not current")
        if plan.target_page_version != tgt.current_version:
            bad("MN-current-version", plan, "target version is not current")
        if plan.target_page_id in src.linked_page_ids:
            bad("MN-already-linked", plan, "source's current version already links the target")

        anchor = plan.anchor_text
        low = anchor.lower().strip()
        if low in geo:
            bad("MN-geo-anchor", plan, "anchor is a geography alias")
        words = len(anchor.split())
        min_words = (
            place.min_anchor_words
            if plan.anchor_source == "target_topic"
            else place.min_anchor_words_non_topic
        )
        if (
            low in forbidden
            or words < min_words
            or words > place.max_anchor_words
            or len(anchor) < place.min_anchor_chars
        ):
            bad("MN-forbidden-anchor", plan, f"anchor rule ({words} words)")
        if plan.block_kind not in place.block_kinds:
            bad("MN-block-kind", plan, f"block kind {plan.block_kind!r}")

        block = locate_block(plan, src)
        if block is None:
            bad("MN-verbatim", plan, "no block of the source version has the context hash")
            continue
        if block.kind != plan.block_kind or block.kind not in place.block_kinds:
            bad("MN-block-kind", plan, f"located block kind {block.kind!r}")
        if block.text[plan.start : plan.end] != anchor:
            bad("MN-verbatim", plan, "anchor is not block_text[start:end]")
        before = [s for s in _occurrences(anchor, block.text) if s < plan.start]
        if plan.occurrence_index != len(before):
            bad("MN-verbatim", plan, f"occurrence_index {plan.occurrence_index} != {len(before)}")
        # Excluded ancestors: the block's enclosing elements and the run under the span.
        enclosing = block.enclosing
        if enclosing & excluded:
            bad("MN-excluded-ancestor", plan, f"block enclosed by {sorted(enclosing & excluded)}")
        runs = block.runs
        if not runs:
            bad("MN-excluded-ancestor", plan, "block has no text runs")
        else:
            inside = [r for r in runs if r.start <= plan.start and plan.end <= r.end]
            if len(inside) != 1:
                bad("MN-excluded-ancestor", plan, "span is not inside exactly one text run")
            elif inside[0].ancestors & excluded:
                bad("MN-excluded-ancestor", plan, f"span under {sorted(inside[0].ancestors)}")
        # Existing links: no overlap, and block capacity (existing + planned).
        existing = src.links_by_block.get(block.block_id, ())
        for text in existing:
            for s in _occurrences(text, block.text) if text else ():
                if _spans_overlap((s, s + len(text)), (plan.start, plan.end)):
                    bad("MN-link-overlap", plan, f"overlaps existing link {text!r}")
        planned_here = planned_spans.get((plan.source_page_id, plan.context_hash), [])
        if plan.status in OPEN and len(existing) + len(planned_here) > select.max_links_per_block:
            bad(
                "MN-link-overlap",
                plan,
                f"block holds {len(existing)} existing + {len(planned_here)} planned links",
            )

        # Score identities and thresholds; raw confidence.
        product = math.prod(plan.factors.values()) if plan.factors else 1.0
        if plan.relevance + 1e-9 < select.min_relevance or plan.total_score + 1e-9 < (
            select.min_score
        ):
            bad(
                "MN-thresholds",
                plan,
                f"relevance {plan.relevance:.3f} total {plan.total_score:.3f}",
            )
        if not math.isclose(plan.total_score, plan.relevance * product, abs_tol=1e-6):
            bad("MN-thresholds", plan, "total != relevance x factors")
        if plan.calibration_version is not None or not math.isclose(
            plan.confidence, plan.total_score, abs_tol=1e-9
        ):
            bad("MN-raw-confidence", plan, "confidence presented as calibrated")

    # Caps and uniqueness over OPEN plans.
    per_run_source: Counter[tuple[str | None, str]] = Counter()
    per_run_target: Counter[tuple[str | None, str]] = Counter()
    pairs: Counter[tuple[str, str]] = Counter()
    starts: Counter[tuple[str, str, int]] = Counter()
    per_source_open: Counter[str] = Counter()
    for plan in open_plans:
        per_run_source[(plan.run_id, plan.source_page_id)] += 1
        per_run_target[(plan.run_id, plan.target_page_id)] += 1
        pairs[(plan.source_page_id, plan.target_page_id)] += 1
        starts[(plan.source_page_id, plan.context_hash, plan.start)] += 1
        per_source_open[plan.source_page_id] += 1
    for (_run, source), n in per_run_source.items():
        if n > select.max_plans_per_source:
            found["MN-per-source"].append(f"{pages[source].canonical_url}: {n} plans in one run")
    for (_run, target), n in per_run_target.items():
        if n > select.max_plans_per_target_per_run:
            found["MN-per-target"].append(f"{pages[target].canonical_url}: {n} plans in one run")
    for (source, target), n in pairs.items():
        if n > 1:
            found["MN-open-pair"].append(f"{source} -> {target}: {n} open plans")
    for key, n in starts.items():
        if n > 1:
            found["MN-open-span"].append(f"{key}: {n} open plans")
    for (source, _hash), spans in planned_spans.items():
        ordered = sorted(spans)
        for (s1, e1, _), (s2, e2, _) in itertools.pairwise(ordered):
            if _spans_overlap((s1, e1), (s2, e2)):
                found["MN-open-span"].append(f"{pages[source].canonical_url}: overlapping spans")
    for source, n in per_source_open.items():
        page = pages[source]
        if page.word_count and (page.internal_body_links + n) / page.word_count * 100 > (
            select.max_internal_links_per_100_words + 1e-9
        ):
            found["MN-density"].append(
                f"{page.canonical_url}: {page.internal_body_links} existing + {n} planned "
                f"over {page.word_count} words"
            )
    return {rule: items for rule, items in found.items() if items}


# --------------------------------------------------------------------------- #
# Quality metrics
# --------------------------------------------------------------------------- #
def _mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return round(math.fsum(items) / len(items), 4) if items else None


def quality_metrics(
    plans: Sequence[PlanRecord],
    *,
    policy: RecommendationPolicy,
    taxonomy: TaxonomyPolicy,
    target_markets: Mapping[str, str | None],
) -> dict[str, Any]:
    """Precision@3, recall of GOOD, BAD rate and anchor exact match (fixture conventions)."""
    cap = policy.selection.max_plans_per_source
    judged = source_judgments()
    open_plans = [p for p in plans if p.status in OPEN]
    by_source: dict[str, list[PlanRecord]] = defaultdict(list)
    for plan in open_plans:
        by_source[plan.source_url].append(plan)

    precision: list[float] = []
    recall: list[float] = []
    labels: Counter[str] = Counter()
    bad_examples: list[str] = []
    unjudged_examples: list[str] = []
    per_source: dict[str, dict[str, Any]] = {}
    for source, verdict in sorted(judged.items()):
        mine = by_source.get(source, [])
        got = [verdict.label(p.target_url) for p in mine]
        labels.update(got)
        hits = sum(1 for g in got if g in ("good", "acceptable"))
        if mine:
            precision.append(hits / len(mine))
        if verdict.good:
            recall.append(sum(1 for g in got if g == "good") / min(len(verdict.good), cap))
        for plan, g in zip(mine, got, strict=True):
            line = f"{plan.source_url} -> {plan.target_url} [{plan.anchor_text!r}]"
            if g == "bad":
                bad_examples.append(f"{line} ({verdict.bad_kind[plan.target_url]})")
            elif g == "unjudged":
                unjudged_examples.append(line)
        per_source[source] = {
            "plans": [
                {
                    "target": p.target_url,
                    "anchor": p.anchor_text,
                    "label": g,
                    "score": p.total_score,
                }
                for p, g in zip(mine, got, strict=True)
            ],
            "good": len(verdict.good),
        }
    judged_plans = sum(labels.values())

    exact: list[bool] = []
    anchor_misses: list[str] = []
    proposed = {(p.source_url, p.target_url): p for p in open_plans}
    expected = judgments()["expected_anchors"]
    for item in expected:
        hit = proposed.get((item["source"], item["target"]))
        if hit is None:
            continue
        exact.append(hit.anchor_text == item["anchor"])
        if hit.anchor_text != item["anchor"]:
            anchor_misses.append(
                f"{item['source']} -> {item['target']}: got {hit.anchor_text!r}, "
                f"expected {item['anchor']!r}"
            )

    on_market = [
        p.anchor_text.lower() in market_phrases(taxonomy, target_markets.get(p.target_page_id))
        for p in open_plans
    ]
    return {
        "plans": len(open_plans),
        "sources_with_plans": len(by_source),
        "judged_sources": len(judged),
        "judged_sources_with_plans": len(precision),
        "precision_at_3_macro": _mean(precision),
        "precision_at_3_micro": round((labels["good"] + labels["acceptable"]) / judged_plans, 4)
        if judged_plans
        else None,
        "recall_good_macro": _mean(recall),
        "bad_rate": round(labels["bad"] / judged_plans, 4) if judged_plans else None,
        "unjudged_rate": round(labels["unjudged"] / judged_plans, 4) if judged_plans else None,
        "label_counts": dict(sorted(labels.items())),
        "anchor_exact_match": round(sum(exact) / len(exact), 4) if exact else None,
        "anchor_pairs_proposed": f"{len(exact)}/{len(expected)}",
        "anchor_names_target_market_rate": round(sum(on_market) / len(on_market), 4)
        if on_market
        else None,
        "anchor_source_counts": dict(sorted(Counter(p.anchor_source for p in open_plans).items())),
        "channel_counts": dict(sorted(Counter(c for p in open_plans for c in p.channels).items())),
        "mean_total_score": _mean(p.total_score for p in open_plans),
        "bad_examples": sorted(bad_examples),
        "unjudged_examples": sorted(unjudged_examples),
        "anchor_mismatches": sorted(anchor_misses),
        "per_source": per_source,
    }
