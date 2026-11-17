"""Retry the rollback until the lock clears, then revalidate and confirm the live sentence."""
from __future__ import annotations

import asyncio
import json
import os
import urllib.request
import uuid

os.environ["KEN_CMS_WRITES_ENABLED"] = "true"

from ken_contracts.cms import CMSUnavailableError  # noqa: E402
from ken_core.config import get_settings  # noqa: E402
from ken_core.providers.cms.factory import build_cms_provider  # noqa: E402

SLUG = "united-arab-emirates-regional-auto-components-import-and-distribution-market"
PAGE = f"{SLUG}:v02_market_overview"
TARGET = "/industry-reports/qatar-tire-distribution-and-retail-service-ecosystem-market"
MARK = f'href="{TARGET}"'


def revalidate() -> str:
    body = json.dumps({"slug": SLUG, "type": "v0.2"}).encode()
    req = urllib.request.Request(
        "https://www.thekenconsulting.com/api/revalidate", data=body, method="POST"
    )
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()[:120].decode("utf-8", "replace")


def live_sentence() -> str:
    req = urllib.request.Request(
        f"https://www.thekenconsulting.com/industry-reports/{SLUG}",
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        t = r.read().decode("utf-8", "replace")
    i = t.find("comparable specifications")
    return t[i : i + 170] if i >= 0 else "(paragraph not found)"


async def main() -> None:
    cms = build_cms_provider(get_settings())
    try:
        for attempt in range(1, 11):
            page = await cms.get_page(page_id=PAGE)
            if MARK not in page.body_html:
                print(f"attempt {attempt}: link is gone from the CMS ({len(page.body_html)} chars)")
                break
            try:
                await cms.rollback(
                    deployment_id="6", operation_id=uuid.uuid4(), actor="abhinav.kumar"
                )
                print(f"attempt {attempt}: rollback accepted")
            except CMSUnavailableError as exc:
                print(f"attempt {attempt}: {exc}")
                await asyncio.sleep(30)
                continue
        page = await cms.get_page(page_id=PAGE)
        print(f"\nCMS now: {len(page.body_html)} chars, link present={MARK in page.body_html}")
        if MARK in page.body_html:
            print("NOT RESTORED -- the link is still in the CMS.")
            return
        print("revalidate:", revalidate())
        await asyncio.sleep(5)
        print("\nLIVE sentence now:\n ", live_sentence())
    finally:
        await cms.aclose()


asyncio.run(main())
