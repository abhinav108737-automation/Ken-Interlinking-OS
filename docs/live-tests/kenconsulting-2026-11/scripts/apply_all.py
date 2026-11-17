# -*- coding: utf-8 -*-
"""Apply one test link per report through the ADR-0028 adapter, then revalidate each page."""
from __future__ import annotations

import asyncio
import html
import json
import os
import re
import urllib.request
import uuid

os.environ["KEN_CMS_WRITES_ENABLED"] = "true"

from ken_contracts.cms import (  # noqa: E402
    LinkChange, LinkChangeRequest, LinkLocator, LinkOperation,
)
from ken_core.config import get_settings  # noqa: E402
from ken_core.providers.cms.factory import build_cms_provider  # noqa: E402

ACTOR = "abhinav.kumar"
MAAS = "united-arab-emirates-mobility-as-a-service-platforms-maas-market"

plan = [p for p in json.load(open("plan.json", encoding="utf-8")) if "error" not in p]
plan.append({
    "source": MAAS, "field": "v02_market_overview", "anchor": "fleet availability",
    "occurrence": 0, "target": "indonesia-commercial-vehicle-leasing-and-fleet-management-market",
    "context": "Dubai is the dominant operating hub ... digital payment scale, fleet availability, and integrated mobility infrastructure.",
})


def revalidate(slug: str, times: int = 6) -> None:
    body = json.dumps({"slug": slug, "type": "v0.2"}).encode()
    for _ in range(times):
        req = urllib.request.Request(
            "https://www.thekenconsulting.com/api/revalidate", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(req, timeout=60).read()
        except Exception as exc:  # noqa: BLE001
            print(f"    revalidate failed: {type(exc).__name__}")


async def main() -> None:
    cms = build_cms_provider(get_settings())
    results = []
    try:
        for item in plan:
            page_id = f"{item['source']}:{item['field']}"
            href = f"/industry-reports/{item['target']}"
            print(f"\n=== {item['source']}  [{item['field']}]")
            print(f"    anchor '{item['anchor']}'  ->  {href}")
            try:
                page = await cms.get_page(page_id=page_id)
                if f'href="{href}"' in page.body_html:
                    print("    already linked; skipped")
                    results.append({**item, "status": "already linked"})
                    continue
                change = LinkChange(
                    change_id=uuid.uuid4(), operation=LinkOperation.INSERT,
                    target_page_id=item["target"], href=href,
                    anchor_text=item["anchor"],
                    locator=LinkLocator(block_selector=item["field"],
                                        occurrence_index=item["occurrence"]),
                )
                request = LinkChangeRequest(
                    operation_id=uuid.uuid4(), source_page_id=page_id,
                    expected_version=page.version, changes=[change],
                    dry_run=True, actor=ACTOR,
                )
                check = await cms.validate_link_changes(request=request)
                if not check.valid:
                    for i in check.issues:
                        print(f"    REFUSED [{i.code}] {i.message}")
                    results.append({**item, "status": "refused"})
                    continue
                res = await cms.apply_link_changes(
                    request=request.model_copy(update={"dry_run": False}))
                after = await cms.get_page(page_id=page_id)
                idx = after.body_html.find(f'href="{href}"')
                ctx = html.unescape(re.sub(r"<[^>]+>", "",
                      after.body_html[max(0, idx - 130):idx + 200])) if idx >= 0 else ""
                print(f"    APPLIED deployment {res.deployment_id}")
                print(f"    now reads: ...{ctx.strip()}...")
                results.append({**item, "status": "applied",
                                "deployment_id": res.deployment_id, "live_context": ctx.strip()})
            except Exception as exc:  # noqa: BLE001
                print(f"    ERROR {type(exc).__name__}: {exc}")
                results.append({**item, "status": f"error: {type(exc).__name__}"})
    finally:
        await cms.aclose()

    print("\n=== revalidating every page ===")
    for slug in sorted({r["source"] for r in results}):
        revalidate(slug)
        print(f"  revalidated {slug}")
    json.dump(results, open("applied.json", "w", encoding="utf-8"), indent=1)


asyncio.run(main())
