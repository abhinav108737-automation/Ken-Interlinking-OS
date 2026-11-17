from __future__ import annotations

import asyncio
import pathlib

from ken_core.config import get_settings
from ken_core.providers.cms.factory import build_cms_provider

SLUG = "united-arab-emirates-regional-auto-components-import-and-distribution-market"
OUT = pathlib.Path(__file__).with_name("cms_fields")


async def main() -> None:
    cms = build_cms_provider(get_settings())
    OUT.mkdir(exist_ok=True)
    try:
        for field in ("v01_executive_summary", "v02_market_overview", "v03_market_size"):
            try:
                page = await cms.get_page(page_id=f"{SLUG}:{field}")
            except Exception as exc:  # noqa: BLE001
                print(f"{field}: {type(exc).__name__}: {exc}")
                continue
            (OUT / f"{field}.html").write_text(page.body_html, encoding="utf-8")
            print(f"{field}: {len(page.body_html)} chars -> {OUT / (field + '.html')}")
    finally:
        await cms.aclose()


asyncio.run(main())
