"""Phase 2 demo-corpus eval: spec-derived analysis goldens vs the implementation.

``tests/fixtures/demo/analysis_expected.json`` was written from ADR-0010 and
``policies/taxonomy/taxonomy.v1.yaml`` BEFORE the analyser was run. Every
record of ``research_site.jsonl`` is read with ``FileExportContentSource``,
normalised by the Phase 1 ``normalize_document`` and then checked against:

* ``analyze_page``: page type, intent, commercial importance, market and
  industry, geographies and entities (at least the expected ones);
* ``match_protections``: the exact set of policy protection rules;
* ``compute_eligibility``: as_source / as_target and the exact reason sets,
  from an ``EligibilityState`` built from the normalised record (published or
  draft, not gone, canonical status, word count, a FRESH analysis).

Plus corpus invariants (the committed JSONL is the generator's output, only
reserved example domains, the special cases exist) and ADR-0010 section 3
properties (immutable versioned analyses, extractive summary, determinism).

No database, no network, no model. See ``evals/intelligence/README.md``.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from typing import Any
from urllib.parse import urlsplit

import pytest

from ken_contracts.intelligence import analysis_version
from ken_contracts.inventory import CanonicalStatus, ExportHeader, ExportPageRecord
from ken_services.intelligence.analyze import MARKET_CONFIDENCE_THRESHOLD, analyze_page
from ken_services.intelligence.eligibility import compute_eligibility
from tests.unit.intelligence_evals import demo_corpus as demo

GOLDENS: dict[str, dict[str, Any]] = demo.load_json(demo.GOLDENS_PATH)["pages"]
SITE_HOST = "research.example.com"
ALLOWED_EXTERNAL_HOST = re.compile(r"(^|\.)example\.(org|net)$")

# The eval once recorded a conflict here: ADR-0010 said "most specific node
# clearing the threshold", the analyser picks the highest-scoring one. The two
# agree on every report and insight; they differed only on hub pages that list
# many sub-markets, where the industry is the editorially right answer. The
# orchestrator adopted the implemented rule and amended ADR-0010 section 3.


def _golden_ids() -> list[str]:
    return sorted(GOLDENS)


def _page(url: str) -> Any:
    pages = demo.pages_by_url()
    assert url in pages, f"golden {url} is not in the demo corpus"
    return pages[url]


# --------------------------------------------------------------------------- #
# Corpus invariants
# --------------------------------------------------------------------------- #
def test_committed_corpus_is_the_generator_output() -> None:
    script = demo.REPO_ROOT / "evals" / "intelligence" / "build_demo_corpus.py"
    result = subprocess.run(  # noqa: S603 - fixed interpreter and repository script, no shell
        [sys.executable, str(script), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_corpus_follows_the_jsonl_export_format() -> None:
    lines = demo.CORPUS_PATH.read_text("utf-8").splitlines()
    header = ExportHeader.model_validate(json.loads(lines[0]))
    assert header.site_url == f"https://{SITE_HOST}/"
    records = [ExportPageRecord.model_validate(json.loads(line)) for line in lines[1:]]
    assert len(records) >= 60
    ids = [r.external_id for r in records]
    assert ids == [f"ken-demo-{n}" for n in range(1, len(records) + 1)]
    assert len({r.url for r in records}) == len(records)


def test_corpus_has_the_required_special_cases() -> None:
    pages = demo.load_pages()
    paths = [urlsplit(p.url).path for p in pages]
    assert sum(path.startswith("/industry-reports/") for path in paths) >= 35
    assert sum(path.startswith("/insights/") for path in paths) >= 12
    assert sum(p.publication_state.value == "draft" for p in pages) == 2
    assert sum(p.metadata.get("cms_protected") == "true" for p in pages) == 1
    assert sum(p.word_count < 60 for p in pages) == 2
    assert sum(p.canonical_status is CanonicalStatus.DECLARED for p in pages) == 1
    for required in ("/about", "/careers", "/privacy", "/terms", "/contact", "/industry-reports"):
        assert required in paths


def test_report_and_insight_bodies_are_realistic() -> None:
    for page in demo.load_pages():
        path = urlsplit(page.url).path
        if not path.startswith(("/industry-reports/", "/insights/")):
            continue
        if page.word_count < 150:  # the deliberate drafts, alias, thin and mid-length cases
            continue
        assert 250 <= page.word_count <= 700, (path, page.word_count)
        assert any(h.level == 2 for h in page.headings), path


def test_only_reserved_example_domains_are_linked() -> None:
    internal = external = 0
    for page in demo.load_pages():
        for link in page.links:
            if link.resolved_url is None:
                continue
            host = urlsplit(link.resolved_url).hostname or ""
            if link.link_type.value == "internal":
                internal += 1
                assert host == SITE_HOST, link.resolved_url
            else:
                external += 1
                assert ALLOWED_EXTERNAL_HOST.search(host), link.resolved_url
    assert internal > 50
    assert external >= 4


def test_internal_links_point_at_corpus_pages() -> None:
    known = set(demo.pages_by_url())
    for page in demo.load_pages():
        for link in page.links:
            if link.link_type.value == "internal":
                assert link.resolved_url in known, (page.url, link.resolved_url)


# --------------------------------------------------------------------------- #
# Analysis goldens
# --------------------------------------------------------------------------- #
def test_goldens_cover_enough_pages() -> None:
    assert len(GOLDENS) >= 25
    with_market = [g for g in GOLDENS.values() if "market_id" in g]
    assert len(with_market) >= 25


@pytest.mark.parametrize("url", _golden_ids())
def test_analysis_matches_golden(url: str) -> None:
    golden = GOLDENS[url]
    analysis = demo.analyses()[url]
    actual = {
        "page_type": analysis.page_type,
        "intent": analysis.intent.value,
        "market_id": analysis.market_id,
        "industry_id": analysis.industry_id,
    }
    expected = {k: golden[k] for k in actual if k in golden}
    assert {k: actual[k] for k in expected} == expected
    geos = {g.geo_id for g in analysis.geographies}
    missing_geos = set(golden.get("geographies_include", ())) - geos
    assert not missing_geos, f"missing geographies {sorted(missing_geos)}; got {sorted(geos)}"
    entities = {e.entity_id for e in analysis.entities}
    missing_entities = set(golden.get("entities_include", ())) - entities
    assert not missing_entities, f"missing entities {sorted(missing_entities)}"


@pytest.mark.parametrize("url", _golden_ids())
def test_protection_rules_match_golden(url: str) -> None:
    assert demo.protection_rule_ids(_page(url)) == GOLDENS[url]["protection_rule_ids"]


@pytest.mark.parametrize("url", _golden_ids())
def test_eligibility_matches_golden(url: str) -> None:
    page = _page(url)
    expected = GOLDENS[url]["eligibility"]
    result = compute_eligibility(
        demo.eligibility_state(page, demo.analyses()[url]), taxonomy=demo.taxonomy()
    )
    assert {
        "as_source": result.as_source,
        "as_target": result.as_target,
        "source_reasons": sorted(r.value for r in result.source_reasons),
        "target_reasons": sorted(r.value for r in result.target_reasons),
    } == {
        "as_source": expected["as_source"],
        "as_target": expected["as_target"],
        "source_reasons": sorted(expected["source_reasons"]),
        "target_reasons": sorted(expected["target_reasons"]),
    }


def test_relatedness_pool_is_exactly_the_eligible_reports_and_insights() -> None:
    """relatedness.json's pool_rule, recomputed: published, canonical, unprotected, >=120 words."""
    rel = demo.load_relatedness()
    eligible = set()
    for url, analysis in demo.analyses().items():
        if analysis.page_type not in {"report", "insight"}:
            continue
        verdict = compute_eligibility(
            demo.eligibility_state(_page(url), analysis), taxonomy=demo.taxonomy()
        )
        if verdict.as_source and verdict.as_target:
            eligible.add(url)
    assert set(rel.pool) == eligible
    for pair in rel.labels:
        assert pair <= eligible, sorted(pair)


# --------------------------------------------------------------------------- #
# ADR-0010 section 3 properties over the whole corpus
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("url", sorted(demo.pages_by_url()))
def test_market_rule_matches_adr(url: str) -> None:
    """market_id = the highest-scoring node clearing the threshold; ties -> deeper, then id."""
    analysis = demo.analyses()[url]
    confident = [t for t in analysis.topics if t.score >= MARKET_CONFIDENCE_THRESHOLD]
    if not confident:
        assert analysis.market_id is None
        return
    best = min(confident, key=lambda t: (-t.score, -t.depth, t.node_id))
    assert analysis.market_id == best.node_id


def test_hubs_take_their_industry_as_market() -> None:
    """The case that settled the rule: a hub listing many sub-markets is about its industry."""
    analyses = demo.analyses()
    assert analyses["https://research.example.com/industries/automotive"].market_id == "automotive"
    assert analyses["https://research.example.com/industries/healthcare"].market_id == "healthcare"


def test_analysis_is_versioned_and_deterministic() -> None:
    policy = demo.taxonomy()
    for page in demo.load_pages():
        first = demo.analyses()[page.url]
        assert first.analysis_version == analysis_version(policy.version)
        assert first.extractor == "deterministic"
        again = analyze_page(demo.analysis_input(page), taxonomy=policy)
        assert again == first


def test_summaries_are_extractive_and_capped() -> None:
    for page in demo.load_pages():
        summary = demo.analyses()[page.url].summary
        assert len(summary) <= 600
        if urlsplit(page.url).path.startswith("/industry-reports/") and page.word_count >= 120:
            assert summary, page.url
            first_sentence = summary.split(". ")[0].rstrip(".")
            assert first_sentence in page.body_text, page.url


def test_commercial_importance_follows_the_page_type_rule() -> None:
    rules = {rule.id: rule for rule in demo.taxonomy().page_types}
    for analysis in demo.analyses().values():
        rule = rules[analysis.page_type]
        assert analysis.intent == rule.intent
        assert analysis.commercial_importance == rule.commercial_importance
