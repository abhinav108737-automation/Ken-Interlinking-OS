"""The synthetic extension corpus and the report's provenance rules."""

from __future__ import annotations

import importlib
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "evals" / "offline_demo")]
build_extension = importlib.import_module("build_extension")
report = importlib.import_module("report")

BASE = ROOT / "tests" / "fixtures" / "demo" / "research_site.jsonl"
EXT = ROOT / "tests" / "fixtures" / "offline_demo" / "synthetic_extension.jsonl"
SITE = "https://research.example.com"


def _records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()][1:]


def test_the_committed_extension_is_current() -> None:
    assert EXT.read_text() == build_extension.render()


def test_everything_in_it_is_labelled_synthetic_and_on_the_reserved_domain() -> None:
    text = EXT.read_text()
    assert "kenresearch" not in text.lower()
    header = json.loads(text.splitlines()[0])
    assert header["site_url"] == f"{SITE}/" and "SYNTHETIC" in header["generator"]
    for record in _records(EXT):
        assert record["url"].startswith(SITE + "/")
        assert record["metadata"]["synthetic"] == "extension"
        for href in re.findall(r'href="([^"]+)"', record["body_html"]):
            assert href.startswith(SITE + "/"), "no link may leave the fictional site"


def test_it_creates_the_requested_situations() -> None:
    base, ext = _records(BASE), _records(EXT)
    urls = {r["url"] for r in base + ext}
    inbound: dict[str, int] = dict.fromkeys(urls, 0)
    for record in base + ext:
        for href in re.findall(r'href="([^"]+)"', record["body_html"]):
            if href in inbound and href != record["url"]:
                inbound[href] += 1
    situations = {r["url"]: r["metadata"]["situation"] for r in ext}
    orphans = [u for u, s in situations.items() if s.startswith("orphan")]
    assert len(orphans) == 3 and all(inbound[u] == 0 for u in orphans)
    assert any("hub:" in s for s in situations.values())
    assert sum("spoke:" in s for s in situations.values()) == 2
    assert [r for r in ext if r["metadata"].get("cms_protected") == "true"]
    assert [r for r in ext if r["publication_state"] == "draft"]
    assert sum(s.startswith("no valid opportunity") for s in situations.values()) == 2
    existing = next(r for r in ext if r["url"].endswith("ev-fleet-electrification-india"))
    assert len(re.findall(r"<a href", existing["body_html"])) == 3
    assert not {r["url"] for r in ext} & {r["url"] for r in base}


def _data(*, provenance: str, with_plan: bool = True) -> dict[str, Any]:
    page = {
        "id": "1",
        "url": f"{SITE}/a",
        "title": "A",
        "state": "published",
        "word_count": 300,
        "provenance": provenance,
        "situation": None,
        "cms_protected": None,
        "page_type": "report",
        "market_id": None,
        "geographies": [],
        "in_degree": 0,
        "out_degree": 0,
        "is_orphan": True,
        "pagerank": None,
        "protected": False,
    }
    other = {**page, "id": "2", "url": f"{SITE}/b", "is_orphan": False}
    plans = (
        [
            {
                "plan_id": "p",
                "status": "proposed",
                "source_url": f"{SITE}/b",
                "target_url": f"{SITE}/a",
                "target_title": "A",
                "anchor_text": "alpha",
                "block_id": "b1",
                "start_offset": 1,
                "end_offset": 6,
                "preview": {"before": "x", "after": "y"},
                "score": {
                    "components": [
                        {"name": "semantic", "value": 1, "weight": 0.3, "contribution": 0.3}
                    ]
                },
                "total_score": 0.6,
                "confidence": 0.6,
                "reason_codes": [],
                "channels": [],
                "relationship_type": None,
                "rationale": "r",
                "critic_verdict": "accept",
                "critic_reasons": [],
                "critic_explanation": "fine",
                "critic_fallback": False,
                "critic_model": "m",
                "autonomy_mode": "shadow",
                "autonomy_decision": "block",
                "would_have_decision": "auto_publish_with_extra_verification",
                "risk_tier": "medium",
                "autonomy_confidence": 0.6,
                "autonomy_blocks": [],
                "critic_outcome": "accept",
            }
        ]
        if with_plan
        else []
    )
    return {
        "database": "ken_app@x",
        "pages": [page, other],
        "plans": plans,
        "rejections": {"no_natural_anchor": 3},
        "links": {"internal": 1},
        "opportunities": {"orphan": 1},
        "side_effects": {"deployments": 0, "deployment_items": 0, "publish_attempts": 0},
        "decisions": {("shadow", "auto_publish_with_extra_verification"): 1},
    }


def test_a_synthetic_only_run_says_there_are_no_real_findings() -> None:
    markdown, _ = report.build(_data(provenance="synthetic_base"))
    assert markdown.startswith("# Interlinking Report")
    assert "SYNTHETIC DEMONSTRATION" in markdown.split("## 1.")[0]
    section = markdown.split("## 10. Findings from authorised real content")[1].split("## 11.")[0]
    assert "**None.**" in section and "alpha" not in section


def test_authorised_pages_are_reported_separately_and_without_gold_metrics() -> None:
    markdown, _ = report.build(_data(provenance="authorized_import"))
    section = markdown.split("## 10. Findings from authorised real content")[1].split("## 11.")[0]
    assert "authorized_import" in section and "alpha" in section
    assert "Gold-judgment metrics do not exist for real content" in section


def test_unlabelled_pages_are_never_counted_as_real() -> None:
    markdown, _ = report.build(_data(provenance="unlabelled"))
    assert "**2 unlabelled pages**" in markdown
    section = markdown.split("## 10. Findings from authorised real content")[1].split("## 11.")[0]
    assert "**None.**" in section
