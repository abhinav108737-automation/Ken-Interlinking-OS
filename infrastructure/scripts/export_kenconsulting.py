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

#: v0.1's field set. The endpoints are the same for both versions; only the field names differ,
#: and asking for a field of the wrong version answers 404 (confirmed by the CMS team
#: 2026-11-19). v0.1 bodies are far smaller -- about 19 kB against v0.2's ~74 kB.
V01_FIELD_ORDER: Final = (
    "product_exe_summary",
    "product_brief_detail",
    "scope_of_report",
    "faq",
    "research_methodology",
)

FIELDS_BY_VERSION: Final = {"v0.2": FIELD_ORDER, "v0.1": V01_FIELD_ORDER}

#: Fields that are never link sources. A table of contents is a list of headings and a
#: methodology note is boilerplate; an anchor in either teaches the reader nothing, and the
#: owner's standing rule is that a link must sit in editorial prose.
EXCLUDED_FIELDS: Final = frozenset(
    {"v02_table_of_contents", "v02_research_methodology", "research_methodology"}
)

GENERATOR: Final = "kenconsulting-export 1"

#: Which product version to export by default. ADR-0028 was written against v0.2, and the
#: 100-report test publishes v0.2, so that stays the default; ``--product-version v0.1`` exports
#: the legacy catalogue, whose field names are above.
PRODUCT_VERSION: Final = "v0.2"

#: How a listing row's version is recognised. Measured 2026-11-19, after the Ken Consulting
#: team's ``canonical_url_registry`` migration: the listing no longer returns a
#: ``product_version`` key at all, and neither does the report payload. What it does return is
#: ``product_code``, and the version is spelled in it -- ``KRV02-0057`` for the seven original
#: demo reports and ``KR-RPT-V02-64650`` for the 499 published since. So the code is the marker,
#: and ``product_version`` is still honoured first for when they put it back.
VERSION_IN_CODE: Final = {"v0.2": "V02", "v0.1": "V01"}


def is_version(row: dict[str, Any], version: str) -> bool:
    """Whether a listing row (or report payload) is of ``version``.

    Two readings, because the API has served both. If the row still carries
    ``product_version`` that is authoritative. Otherwise the version marker has to be read out
    of ``product_code``; for v0.1 that is an ABSENCE of a v0.2 marker rather than a ``V01``
    string, which no observed code contains.
    """
    declared = row.get("product_version")
    if isinstance(declared, str) and declared:
        return declared == version
    code = str(row.get("product_code") or "").upper()
    if not code:
        return False
    if version == "v0.2":
        return "V02" in code
    return "V02" not in code


ATTEMPTS: Final = 4
BACKOFF_S: Final = (2.0, 6.0, 15.0)


async def fetch(client: httpx.AsyncClient, path: str, token: str) -> Any:
    """GET with retry on transient faults (timeout, connection error, HTTP 429 / 5xx).

    The CMS is slow under load -- a single 60 s read timeout used to discard a whole export.
    A 4xx other than 429 is a real answer and is raised at once. Read-only, so retrying is safe.
    """
    last = "no attempt"
    for attempt in range(ATTEMPTS):
        try:
            response = await client.get(path, headers={"Authorization": f"Bearer {token}"})
        except httpx.TransportError as exc:  # timeouts and connection faults
            last = type(exc).__name__
        else:
            if response.status_code == 200:
                return response.json()
            last = f"HTTP {response.status_code}"
            if response.status_code != 429 and response.status_code < 500:
                break
        if attempt < ATTEMPTS - 1:
            await asyncio.sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
    raise RuntimeError(f"CMS request failed ({last}) for {path}")


async def report_by_field(
    client: httpx.AsyncClient, slug: str, token: str, version: str
) -> dict[str, Any]:
    """Rebuild a report from its per-FIELD endpoint, for when the report endpoint is down.

    ``/internal/reports/<slug>/`` currently answers 500 for some products -- the CMS's own
    debug page blames a missing ``canonical_url_registry`` table -- while
    ``/internal/pages/<slug>:<field>/`` is unaffected. The field list is not discoverable in
    that case, so the version's known field names are tried and the 404s ignored.
    """
    fields: list[dict[str, Any]] = []
    head: dict[str, Any] = {}
    for name in FIELDS_BY_VERSION.get(version, ()):
        try:
            page = await fetch(client, f"/internal/pages/{slug}:{name}/", token)
        except RuntimeError:
            continue  # absent on this product, or still failing: the caller reports the gap
        head = head or page
        fields.append({"field": name, "body_html": page.get("body_html") or ""})
    if not fields:
        raise RuntimeError(f"no field of {slug} could be read")
    return {
        "slug": slug,
        "product_code": head.get("product_code") or slug,
        "product_version": head.get("product_version") or version,
        "url": head.get("url") or "",
        "title": head.get("title") or slug,
        "protected": bool(head.get("protected", False)),
        "fields": fields,
        "recovered_by_field": True,
    }


def body_of(report: dict[str, Any]) -> tuple[str, list[str]]:
    """The report's prose, and the field names that contributed, in order."""
    fields = report.get("fields") or []
    by_name = {f["field"]: f for f in fields if isinstance(f, dict) and f.get("field")}
    default = FIELDS_BY_VERSION.get(str(report.get("product_version") or ""), FIELD_ORDER)
    order = [f["field"] for f in fields if f.get("field")] or list(default)
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
    # Re-measured 2026-11-19 after their migration: the superlinear latency is GONE -- limit=25,
    # 100 and 200 all answer in about 0.2 s, where limit=200 previously timed out at 60 s. 100 is
    # the default rather than 200 so a single slow page cannot cost the whole walk.
    parser.add_argument("--limit", type=int, default=100, help="Page size, max 200.")
    parser.add_argument(
        "--slug",
        action="append",
        default=None,
        dest="slugs",
        help="Export only these slugs (repeatable). Skips the catalogue walk entirely.",
    )
    parser.add_argument(
        "--product-version",
        default=PRODUCT_VERSION,
        help=f"Only export products of this version (default {PRODUCT_VERSION}).",
    )
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
        seen = 0
        cursor: str | None = None
        while args.slugs is None:
            path = f"/internal/pages/?limit={size}" + (f"&after={cursor}" if cursor else "")
            listing = await fetch(client, path, token)
            results = listing.get("results", [])
            seen += len(results)
            # The endpoint ignores every filter query parameter tried (product_version, version,
            # type all return v0.1 rows), so the version filter has to be applied here.
            slugs += [
                r["slug"] for r in results if r.get("slug") and is_version(r, args.product_version)
            ]
            cursor = listing.get("next_after")
            if not cursor:
                break
        if args.slugs is not None:
            slugs = list(dict.fromkeys(args.slugs))
            print(f"{len(slugs)} slug(s) given; the catalogue walk was skipped")
        else:
            print(f"{seen} products in the CMS; {len(slugs)} are {args.product_version}")
        if not slugs:
            print(f"no {args.product_version} product found: nothing to export", file=sys.stderr)
            return 2

        records: list[dict[str, Any]] = []
        host = ""
        failed: list[str] = []
        recovered = 0
        for slug in slugs:
            try:
                report = await fetch(client, f"/internal/reports/{slug}/", token)
            except RuntimeError as exc:
                try:
                    report = await report_by_field(client, slug, token, args.product_version)
                except RuntimeError:
                    failed.append(slug)
                    print(f"  FAILED {slug}: {exc}", file=sys.stderr)
                    continue
                recovered += 1
                print(f"  recovered {slug} field by field ({exc})", file=sys.stderr)
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
    if recovered:
        print(f"{recovered} of them were recovered field by field (report endpoint failing)")
    if failed:
        # A partial export must never pass for a complete one: the pages missing here would
        # silently be absent as link TARGETS and SOURCES for the whole run.
        print(f"INCOMPLETE: {len(failed)} report(s) could not be read: {failed}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
