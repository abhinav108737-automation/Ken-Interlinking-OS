"""Read-only: what URL does the CMS claim for the source report, and which fields exist?"""
from __future__ import annotations

import asyncio

from ken_core.config import get_settings
from ken_core.providers.cms.factory import build_cms_provider

SLUG = "united-arab-emirates-regional-auto-components-import-and-distribution-market"
FIELD = "v02_market_overview"


async def main() -> None:
    settings = get_settings()
    print(f"base_url {settings.cms_base_url}")
    cms = build_cms_provider(settings)
    try:
        page = await cms.get_page(page_id=f"{SLUG}:{FIELD}")
        print(f"page_id  {page.page_id}")
        print(f"url      {page.url}")
        print(f"title    {page.title}")
    finally:
        await cms.aclose()


asyncio.run(main())
