"""Read-only discovery of new contextual-link evidence in the v0.2 report set."""

from __future__ import annotations

import asyncio
import csv
import json
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[2]
CSV_PATH = ROOT / "data" / "v02-contextual-review" / "v02_reports_test_interlinking.csv"
PROPOSALS_PATH = ROOT / "data" / "v02-contextual-review" / "proposals.json"
OUTPUT_PATH = ROOT / "outputs" / "contextual-v02-shadow" / "new_candidate_evidence.json"
ALLOWED_FIELDS = {
    "v02_growth_drivers",
    "v02_market_trajectory",
    "v02_market_segmentation",
    "v02_regional_analysis",
}
STOP = {
    "a",
    "ai",
    "and",
    "based",
    "cloud",
    "for",
    "market",
    "markets",
    "platform",
    "platforms",
    "software",
    "system",
    "systems",
    "the",
}
NEGATIVE = re.compile(
    r"\b(does not|do not|cannot|unverified|not establish|not separately|disclaimer|excluded)\b",
    re.IGNORECASE,
)


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


def visible_prose(report: dict[str, object]) -> list[dict[str, str]]:
    prose: list[dict[str, str]] = []
    for field in report.get("fields", []):
        if not isinstance(field, dict) or field.get("field") not in ALLOWED_FIELDS:
            continue
        soup = BeautifulSoup(str(field.get("body_html") or ""), "html.parser")
        for element in soup.find_all(["p", "li"]):
            if element.find_parent("table") is not None:
                continue
            text = " ".join(element.get_text(" ", strip=True).split())
            if len(words(text)) < 12:
                continue
            links = [str(link.get("href") or "").rstrip("/") for link in element.find_all("a")]
            prose.append(
                {
                    "field": str(field["field"]),
                    "tag": element.name,
                    "text": text,
                    "links": links,
                    "version": str(field.get("version") or ""),
                }
            )
    return prose


def best_anchor(text: str, target_core: str) -> str:
    target_words = words(target_core.replace("cloud-based", " "))
    best = ""
    for length in range(min(7, len(target_words)), 1, -1):
        for start in range(len(target_words) - length + 1):
            phrase = " ".join(target_words[start : start + length])
            if len(useful(phrase)) < 2:
                continue
            match = re.search(rf"\b{re.escape(phrase)}\b", text, re.IGNORECASE)
            if match and len(match.group(0)) > len(best):
                best = match.group(0)
    return best


async def fetch_reports(rows: list[dict[str, str]]) -> dict[str, dict[str, object]]:
    env = read_env()
    base = env.get("KEN_CMS_BASE_URL", "").rstrip("/")
    if urlparse(base).hostname not in {"www.thekenconsulting.com", "thekenconsulting.com"}:
        raise RuntimeError("Unexpected CMS host")
    token = env.get("KEN_CMS_READ_TOKEN")
    if not token:
        raise RuntimeError("CMS read token is missing")
    semaphore = asyncio.Semaphore(8)
    reports: dict[str, dict[str, object]] = {}

    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:

        async def fetch(row: dict[str, str]) -> None:
            async with semaphore:
                response = await client.get(f"{base}/internal/reports/{row['slug']}/")
                response.raise_for_status()
                reports[row["slug"]] = response.json()

        await asyncio.gather(*(fetch(row) for row in rows))
    return reports


async def main() -> None:
    with CSV_PATH.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    proposals = json.loads(PROPOSALS_PATH.read_text(encoding="utf-8"))["proposals"]
    reviewed_pairs = {
        (
            urlparse(item["source_url"]).path.rstrip("/").split("/")[-1],
            urlparse(item["destination_url"]).path.rstrip("/").split("/")[-1],
        )
        for item in proposals
        if item.get("kind") == "contextual"
    }
    by_core: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        if row["market_core"].strip():
            by_core.setdefault(row["market_core"].strip().casefold(), []).append(row)
    regional_groups = [
        group
        for group in by_core.values()
        if len(group) > 1 and len({item["geography"] for item in group}) > 1
    ]
    selected = {row["slug"]: row for row in rows}
    reports = await fetch_reports(list(selected.values()))
    prose = {slug: visible_prose(report) for slug, report in reports.items()}

    contextual: list[dict[str, object]] = []
    by_geography: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        geography = row["geography"].strip().casefold()
        if geography:
            by_geography.setdefault(geography, []).append(row)
    for same_geography in by_geography.values():
        for source in same_geography:
            for target in same_geography:
                pair = (source["slug"], target["slug"])
                if source is target:
                    continue
                destination_path = f"/industry-reports/{target['slug']}"
                target_terms = useful(target["market_core"])
                if len(target_terms) < 2:
                    continue
                for block in prose[source["slug"]]:
                    if destination_path in block["links"] or NEGATIVE.search(block["text"]):
                        continue
                    shared = sorted(useful(block["text"]) & target_terms)
                    anchor = best_anchor(block["text"], target["market_core"])
                    if len(shared) < 2 or len(useful(anchor)) < 2:
                        continue
                    contextual.append(
                        {
                            "source": source,
                            "target": target,
                            "field": block["field"],
                            "tag": block["tag"],
                            "source_version": block["version"],
                            "anchor": anchor,
                            "shared_terms": shared,
                            "source_evidence": block["text"],
                            "discovery_score": round(len(shared) / len(target_terms), 3),
                            "pair_reviewed_before": pair in reviewed_pairs,
                        }
                    )

    regional: list[dict[str, object]] = []
    for group in regional_groups:
        for source in group:
            for target in group:
                if source is target:
                    continue
                target_geo = target["geography"].replace("-", " ")
                for block in prose[source["slug"]]:
                    if block["field"] != "v02_regional_analysis" or NEGATIVE.search(block["text"]):
                        continue
                    if not re.search(rf"\b{re.escape(target_geo)}\b", block["text"], re.IGNORECASE):
                        continue
                    regional.append(
                        {
                            "source": source,
                            "target": target,
                            "field": block["field"],
                            "tag": block["tag"],
                            "source_version": block["version"],
                            "source_evidence": block["text"],
                            "target_geography_mention": target_geo,
                        }
                    )

    def sort_key(item: dict[str, object]) -> tuple[float, str, str]:
        return (
            -float(item["discovery_score"]),
            str(item["source"]["slug"]),
            str(item["target"]["slug"]),
        )

    contextual.sort(key=sort_key)
    result = {
        "mode": "shadow_read_only",
        "reports_fetched": len(reports),
        "reviewed_pairs_excluded": len(reviewed_pairs),
        "contextual_evidence_matches": contextual,
        "regional_evidence_matches": regional,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    output_text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    OUTPUT_PATH.write_text(output_text, encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(OUTPUT_PATH),
                "reports_fetched": len(reports),
                "contextual_matches": len(contextual),
                "regional_matches": len(regional),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
