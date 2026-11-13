"""Export the Ken Consulting CMS to a ``ken-inventory-export`` v1 JSONL file.

Reads every report through the internal API (ADR-0028) and writes one record per REPORT,
whose ``body_html`` is the report's fields concatenated in their published order.

**Why one record per report and not one per field.** A link's destination is a report, and a
report's market and geography are properties of the whole document -- a reader does not visit
``v02_regional_analysis``, they visit the report. Ingesting fields as separate pages would make
the engine analyse a fragment and would make every link point at a fragment URL. The field only
matters at the moment of writing, and it is recovered then by
:mod:`ken_services.deployment.field_resolution`, which looks for the plan's recorded context in
each field.

Because the body we analyse is exactly the bytes the CMS holds, block offsets, context hashes
and anchor text all refer to the text the edit will apply to. That is what removes the
template-parity problem the handover document raised in October.

Read-only: this script never writes to the CMS. Usage::

    python infrastructure/scripts/export_kenconsulting.py --out demo.jsonl [--limit N]

Settings come from ``KEN_CMS_*`` as usual; no token is ever printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import httpx

from ken_core.config import ConfigurationError, get_settings

#: The order fields are concatenated in. Their own order in the API response is authoritative;
#: this is only the fallback when a report omits one.
FIELD_ORDER: Final = (
    "v02_market_overview",
    "v02_scope_of_report",
    "v02_market_trajectory",
    "v02_market_breakdown",
    "v02_market_segmentation",
    "v02_regional_analysis",
    "v02_growth_drivers",
    "v02_competitive_landscape",
    "v02_key_stakeholders",
    "v02_research_methodology",
    "v02_faq",
    "v02_table_of_contents",
)

#: Fields that are never link sources. A table of contents is a list of headings and a
#: methodology note is boilerplate; an anchor in either teaches the reader nothing, and the
#: owner's standing rule is that a link must sit in editorial prose.
EXCLUDED_FIELDS: Final = frozenset({"v02_table_of_contents", "v02_research_methodology"})

GENERATOR: Final = "kenconsulting-export 1"


async def fetch(client: httpx.AsyncClient, path: str, token: str) -> Any:
    response = await client.get(path, headers={"Authorization": f"Bearer {token}"})
    if response.status_code != 200:
        raise RuntimeError(f"CMS answered HTTP {response.status_code} for {path}")
    return response.json()


def body_of(report: dict[str, Any]) -> tuple[str, list[str]]:
    """The report's prose, and the field names that contributed, in order."""
    fields = report.get("fields") or []
    by_name = {f["field"]: f for f in fields if isinstance(f, dict) and f.get("field")}
    order = [f["field"] for f in fields if f.get("field")] or list(FIELD_ORDER)
    parts: list[str] = []
    used: list[str] = []
    for name in order:
        field = by_name.get(name)
        if field is None or name in EXCLUDED_FIELDS:
            continue
        html = field.get("body_html")
        if not isinstance(html, str) or not html.strip():
            continue
        parts.append(html)
        used.append(name)
    return "".join(parts), used


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="JSONL file to write.")
    # Their list endpoint answers 400 above a cap (500 is refused, 200 is accepted), and it
    # pages with `next_after`, so the page size is followed until the cursor runs out.
    parser.add_argument("--limit", type=int, default=200, help="Page size, max 200.")
    args = parser.parse_args()

    try:
        settings = get_settings()
    except ConfigurationError:
        print("configuration error: invalid KEN_* settings", file=sys.stderr)
        return 2
    base = (settings.cms_base_url or "").rstrip("/")
    token = settings.cms_read_token.get_secret_value() if settings.cms_read_token else ""
    if not base or not token:
        print("KEN_CMS_BASE_URL and KEN_CMS_READ_TOKEN must be set", file=sys.stderr)
        return 2

    async with httpx.AsyncClient(
        base_url=base, timeout=settings.cms_timeout_s * 6, follow_redirects=False, trust_env=False
    ) as client:
        size = max(1, min(args.limit, 200))
        slugs: list[str] = []
        cursor: str | None = None
        while True:
            path = f"/internal/pages/?limit={size}" + (f"&after={cursor}" if cursor else "")
            listing = await fetch(client, path, token)
            slugs += [r["slug"] for r in listing.get("results", []) if r.get("slug")]
            cursor = listing.get("next_after")
            if not cursor:
                break
        print(f"{len(slugs)} reports in the CMS")

        records: list[dict[str, Any]] = []
        host = ""
        for slug in slugs:
            report = await fetch(client, f"/internal/reports/{slug}/", token)
            body, used = body_of(report)
            url = str(report.get("url") or "")
            if not url or not body:
                print(f"  SKIPPED {slug}: no url or no body", file=sys.stderr)
                continue
            host = host or httpx.URL(url).host
            records.append(
                {
                    "url": url,
                    "body_html": body,
                    "external_id": str(report.get("product_code") or slug),
                    "page_type": "report",
                    "title": str(report.get("title") or slug),
                    "publication_state": "published",
                    "language": "en",
                    "metadata": {
                        "slug": slug,
                        "fields": ",".join(used),
                        "protected": str(bool(report.get("protected", False))).lower(),
                    },
                }
            )
            print(f"  {slug}  {len(body):>6} chars  {len(used)} fields")

    header = {
        "format": "ken-inventory-export",
        "version": 1,
        "site_url": f"https://{host}/",
        "allowed_hosts": [host],
        "exported_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "generator": GENERATOR,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(header, sort_keys=True) + "\n")
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    print(f"\nwrote {len(records)} pages to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
