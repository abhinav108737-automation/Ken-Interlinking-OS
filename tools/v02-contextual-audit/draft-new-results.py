"""Audit five new v0.2 contextual-link drafts against current CMS evidence."""

from __future__ import annotations

import asyncio
import csv
import json
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup, Tag

ROOT = Path(__file__).resolve().parents[2]
CSV_PATH = ROOT / "data" / "v02-contextual-review" / "v02_reports_test_interlinking.csv"
OUTPUT_DIR = ROOT / "outputs" / "contextual-v02-shadow"
OUTPUT_JSON = OUTPUT_DIR / "new_eligible_interlinking_drafts.json"
OUTPUT_MD = OUTPUT_DIR / "new_eligible_interlinking_drafts.md"
ALLOWED_FIELDS = {
    "v02_growth_drivers",
    "v02_market_trajectory",
    "v02_market_segmentation",
    "v02_regional_analysis",
}
STOP = {
    "a",
    "and",
    "based",
    "cloud",
    "for",
    "kuwait",
    "market",
    "markets",
    "mexico",
    "platform",
    "platforms",
    "software",
    "system",
    "systems",
    "the",
}
PUSHY = re.compile(
    r"\b(click here|read more|learn more|must-read|do not miss|act now|exclusive|unlock)\b|!{2,}",
    re.IGNORECASE,
)
NEGATIVE = re.compile(
    r"\b(does not|do not|cannot|unverified|not establish|not separately|disclaimer|excluded)\b",
    re.IGNORECASE,
)
CANDIDATES = [
    {
        "id": "v02-new:1",
        "source": "kuwait-electrosurgical-devices-market",
        "target": "kuwait-electrosurgical-generators-market",
        "field": "v02_market_segmentation",
        "anchor": "Electrosurgical Generators",
        "find_anchor": "Electrosurgical Generators",
        "must_contain": "console standardization",
        "reason": (
            "The paragraph identifies generators as the largest solution segment and explains "
            "their procurement, standardization and installed-base value."
        ),
    },
    {
        "id": "v02-new:2",
        "source": "mexico-cold-chain-and-food-storage-market",
        "target": "mexico-pharmaceutical-logistics-market",
        "field": "v02_growth_drivers",
        "anchor": "Pharmaceutical logistics",
        "find_anchor": "Pharmaceutical logistics",
        "must_contain": "validated handling",
        "reason": (
            "The paragraph explains why pharmaceutical logistics creates differentiated demand "
            "for validated handling across multiple temperature environments."
        ),
    },
    {
        "id": "v02-new:3",
        "source": "kuwait-germanium-market",
        "target": "kuwait-fiber-optics-market",
        "field": "v02_market_segmentation",
        "anchor": "Fiber Optics",
        "find_anchor": "Fiber Optics",
        "must_contain": "selected fiber design contains germanium",
        "reason": (
            "The paragraph explains the material-to-end-market relationship: fiber expansion can "
            "create demand for germanium compounds when the selected design uses germanium."
        ),
    },
    {
        "id": "v02-new:4",
        "source": "kuwait-female-infertility-diagnosis-market",
        "target": "kuwait-genetic-testing-market",
        "field": "v02_growth_drivers",
        "anchor": "genetic testing",
        "find_anchor": "genetic testing",
        "must_contain": "specialist referral models",
        "reason": (
            "The paragraph describes genetic testing as a specialist referral route for clinics "
            "that do not operate a full genetic laboratory."
        ),
    },
    {
        "id": "v02-new:5",
        "source": "kuwait-funeral-homes-market",
        "target": "kuwait-funeral-and-repatriation-services-market",
        "field": "v02_market_segmentation",
        "anchor": "Repatriation Services",
        "find_anchor": "Repatriation Services",
        "must_contain": "case coordination",
        "reason": (
            "The paragraph distinguishes repatriation as a separate operating capability built "
            "around case coordination, giving readers a clear reason to open the specialist report."
        ),
    },
    {
        "id": "v02-new:6",
        "source": "latin-america-cargo-security-market",
        "target": "mexico-cargo-security-market",
        "field": "v02_regional_analysis",
        "anchor": "Mexico Cargo Security Market",
        "find_anchor": "Mexico",
        "must_contain": "closest commercial comparator",
        "reason": (
            "The regional paragraph directly compares Mexico on market scale and cross-border "
            "freight. Naming the full destination market makes the comparison clear."
        ),
    },
    {
        "id": "v02-new:7",
        "source": "latin-america-warehousing-market",
        "target": "mexico-warehousing-market",
        "field": "v02_regional_analysis",
        "anchor": "Mexico Warehousing Market",
        "find_anchor": "Mexico",
        "must_contain": "ranks second and grows faster",
        "reason": (
            "The regional paragraph directly compares Mexico's ranking, forecast growth, "
            "manufacturing role and cross-border inventory using the same revenue definition."
        ),
    },
]
KPI_DEFINITIONS = [
    ("reader_need", "Reader need", 15),
    ("source_evidence", "Source evidence", 15),
    ("destination_fit", "Destination fit", 20),
    ("anchor_expectation", "Anchor expectation", 15),
    ("placement", "Context and placement", 15),
    ("added_value", "Added information value", 10),
    ("quality_risk", "Quality and risk", 10),
]
EDITORIAL_SCORES = {
    "v02-new:1": ([14, 15, 19, 15, 14, 9, 10], 95),
    "v02-new:2": ([14, 15, 19, 15, 15, 9, 10], 95),
    "v02-new:3": ([15, 15, 19, 15, 14, 10, 9], 94),
    "v02-new:4": ([14, 15, 17, 15, 15, 9, 9], 88),
    "v02-new:5": ([15, 15, 20, 15, 14, 10, 10], 97),
    "v02-new:6": ([15, 15, 20, 15, 15, 10, 8], 96),
    "v02-new:7": ([15, 15, 19, 15, 15, 10, 8], 94),
}


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.casefold())


def root(word: str) -> str:
    if len(word) > 5 and word.endswith("ies"):
        return f"{word[:-3]}y"
    if len(word) > 5 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 4 and word.endswith("s"):
        return word[:-1]
    return word


def useful(text: str) -> set[str]:
    return {root(word) for word in words(text) if len(word) > 2 and word not in STOP}


def visible_elements(field: dict[str, object]) -> list[Tag]:
    soup = BeautifulSoup(str(field.get("body_html") or ""), "html.parser")
    return [
        element
        for element in soup.find_all(["p", "li"])
        if element.find_parent("table") is None
        and len(words(element.get_text(" ", strip=True))) >= 12
    ]


def plain(element: Tag) -> str:
    return " ".join(element.get_text(" ", strip=True).split())


def has_existing_inline_link(report: dict[str, object], destination_path: str) -> bool:
    for field in report.get("fields", []):
        if not isinstance(field, dict) or field.get("field") not in ALLOWED_FIELDS:
            continue
        for element in visible_elements(field):
            for link in element.find_all("a"):
                href = str(link.get("href") or "").rstrip("/")
                if href == destination_path or href.endswith(destination_path):
                    return True
    return False


def source_evidence(
    report: dict[str, object], candidate: dict[str, str]
) -> tuple[dict[str, object], Tag] | None:
    field = next(
        (
            item
            for item in report.get("fields", [])
            if isinstance(item, dict) and item.get("field") == candidate["field"]
        ),
        None,
    )
    if field is None:
        return None
    for element in visible_elements(field):
        text = plain(element)
        if candidate["find_anchor"].casefold() not in text.casefold():
            continue
        if candidate["must_contain"].casefold() not in text.casefold():
            continue
        return field, element
    return None


def target_evidence(report: dict[str, object], desired: str) -> tuple[str, str, list[str]] | None:
    wanted = useful(desired)
    choices: list[tuple[int, str, str, list[str]]] = []
    for field in report.get("fields", []):
        if not isinstance(field, dict) or field.get("field") not in ALLOWED_FIELDS:
            continue
        for element in visible_elements(field):
            text = plain(element)
            if NEGATIVE.search(text) or "comprehensive analysis of key factors" in text.casefold():
                continue
            shared = sorted(useful(text) & wanted)
            choices.append((len(shared), str(field["field"]), text, shared))
    choices.sort(key=lambda item: (-item[0], len(item[2])))
    if not choices or choices[0][0] < 2:
        return None
    _, field_name, text, shared = choices[0]
    return field_name, text, shared


async def fetch(slugs: set[str]) -> dict[str, dict[str, object]]:
    env = read_env()
    base = env.get("KEN_CMS_BASE_URL", "").rstrip("/")
    if urlparse(base).hostname not in {"www.thekenconsulting.com", "thekenconsulting.com"}:
        raise RuntimeError("Unexpected CMS host")
    token = env.get("KEN_CMS_READ_TOKEN")
    if not token:
        raise RuntimeError("CMS read token is missing")
    headers = {"Authorization": f"Bearer {token}"}
    reports: dict[str, dict[str, object]] = {}
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:

        async def one(slug: str) -> None:
            response = await client.get(f"{base}/internal/reports/{slug}/")
            response.raise_for_status()
            reports[slug] = response.json()

        await asyncio.gather(*(one(slug) for slug in slugs))
    return reports


async def main() -> None:
    with CSV_PATH.open(encoding="utf-8-sig", newline="") as handle:
        inventory = {row["slug"]: row for row in csv.DictReader(handle)}
    slugs = {item[key] for item in CANDIDATES for key in ("source", "target")}
    reports = await fetch(slugs)
    results: list[dict[str, object]] = []
    for candidate in CANDIDATES:
        source_row = inventory[candidate["source"]]
        target_row = inventory[candidate["target"]]
        source_report = reports[candidate["source"]]
        target_report = reports[candidate["target"]]
        destination_path = f"/industry-reports/{candidate['target']}"
        found = source_evidence(source_report, candidate)
        duplicate = has_existing_inline_link(source_report, destination_path)
        target_found = None
        if found:
            _, source_element = found
            desired = f"{target_row['market_core']} {plain(source_element)}"
            target_found = target_evidence(target_report, desired)
        source_text = plain(found[1]) if found else ""
        anchor_terms = useful(candidate["anchor"])
        target_terms = useful(target_row["market_core"])
        regional_anchor = (
            target_row["geography"].replace("-", " ").casefold() in candidate["anchor"].casefold()
            and len(anchor_terms & target_terms) >= 1
        )
        checks = {
            "allowed_section": candidate["field"] in ALLOWED_FIELDS,
            "visible_explanatory_prose": bool(found),
            "not_already_linked_inline": not duplicate,
            "descriptive_anchor": (len(anchor_terms) >= 2 and len(anchor_terms & target_terms) >= 2)
            or regional_anchor,
            "destination_evidence": target_found is not None,
            "neutral_non_pushy": not PUSHY.search(source_text),
            "no_negative_or_disclaimer_context": not NEGATIVE.search(source_text),
        }
        passed = all(checks.values())
        score_values, confidence = EDITORIAL_SCORES[candidate["id"]]
        kpis = [
            {"key": key, "label": label, "score": score, "maximum": maximum}
            for (key, label, maximum), score in zip(KPI_DEFINITIONS, score_values, strict=True)
        ]
        total_score = sum(score_values)
        score_decision = (
            "approve"
            if passed and total_score >= 85
            else "revise"
            if passed and total_score >= 70
            else "reject"
            if found and target_found
            else "insufficient_evidence"
        )
        field = found[0] if found else {}
        after = source_text
        if passed:
            after = re.sub(
                re.escape(candidate["find_anchor"]),
                f"[{candidate['anchor']}]({target_row['url']})",
                source_text,
                count=1,
                flags=re.IGNORECASE,
            )
        results.append(
            {
                "id": candidate["id"],
                "verdict": "draft_ready" if passed else "refuse_to_draft",
                "checks": checks,
                "source_report": source_row["title"],
                "source_url": source_row["url"],
                "destination_report": target_row["title"],
                "destination_url": target_row["url"],
                "source_field": candidate["field"],
                "source_version": str(field.get("version") or ""),
                "anchor": candidate["anchor"],
                "exact_find": source_text,
                "before_copy": source_text,
                "after_copy": after,
                "copy_change": None
                if candidate["find_anchor"] == candidate["anchor"]
                else {
                    "operation": "replace",
                    "find": source_text,
                    "new_text": after,
                },
                "reader_value": candidate["reason"],
                "source_evidence": source_text,
                "target_evidence_field": target_found[0] if target_found else None,
                "target_evidence": target_found[1] if target_found else "",
                "target_shared_terms": target_found[2] if target_found else [],
                "cms_validation": "not_run_shadow_only",
                "scorer": {
                    "version": "contextual-editorial-v1",
                    "label": "Independent editorial reference",
                    "decision": score_decision,
                    "total": total_score,
                    "maximum": 100,
                    "confidence": confidence,
                    "kpis": kpis,
                    "hard_gates": [
                        {"label": "Source phrase exists", "status": "pass" if found else "fail"},
                        {
                            "label": "Destination evidence exists",
                            "status": "pass" if target_found else "fail",
                        },
                        {
                            "label": "Approved visible section",
                            "status": "pass" if checks["allowed_section"] else "fail",
                        },
                        {
                            "label": "No existing inline duplicate",
                            "status": "pass" if checks["not_already_linked_inline"] else "fail",
                        },
                        {
                            "label": "Rendered crawlable link",
                            "status": "pending",
                        },
                    ],
                    "missing_information": [
                        "Rendered <a href> validation remains pending until an approved "
                        "dry run and apply."
                    ],
                    "disclaimer": (
                        "Editorial reference only. It does not reproduce Google ranking systems "
                        "or predict AI citations."
                    ),
                },
            }
        )
    output = {
        "schema_version": 1,
        "mode": "shadow_read_only",
        "summary": {
            "reviewed": len(results),
            "draft_ready": sum(item["verdict"] == "draft_ready" for item in results),
            "refused": sum(item["verdict"] == "refuse_to_draft" for item in results),
        },
        "results": results,
    }
    markdown = [
        "# New eligible v0.2 contextual interlinking drafts",
        "",
        "These drafts use current CMS evidence and do not change source copy. "
        "No CMS write was performed.",
        "",
    ]
    for item in results:
        markdown.extend(
            [
                f"## {item['id']}: {item['source_report']} → {item['destination_report']}",
                "",
                f"- Verdict: **{item['verdict']}**",
                f"- Placement: `{item['source_field']}`",
                f"- Anchor: **{item['anchor']}**",
                f"- Reader value: {item['reader_value']}",
                f"- Before: {item['before_copy']}",
                f"- Draft: {item['after_copy']}",
                f"- Target evidence: {item['target_evidence']}",
                "",
            ]
        )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_json = json.dumps(output, ensure_ascii=False, indent=2) + "\n"
    OUTPUT_JSON.write_text(output_json, encoding="utf-8")
    OUTPUT_MD.write_text("\n".join(markdown), encoding="utf-8")
    print(json.dumps(output["summary"], indent=2))


if __name__ == "__main__":
    asyncio.run(main())
