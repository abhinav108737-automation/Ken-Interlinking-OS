"""Read-only: what does the live CMS hold on that page right now?"""
from __future__ import annotations

import asyncio

from ken_core.config import get_settings
from ken_core.providers.cms.factory import build_cms_provider

PAGE = (
    "united-arab-emirates-regional-auto-components-import-and-distribution-market"
    ":v02_market_overview"
)
TARGET = "/industry-reports/qatar-tire-distribution-and-retail-service-ecosystem-market"
ANCHOR = "Qatar tire distribution market"


async def main() -> None:
    cms = build_cms_provider(get_settings())
    try:
        page = await cms.get_page(page_id=PAGE)
        body = page.body_html
        print(f"version   {page.version}")
        print(f"protected {page.protected}")
        print(f"body      {len(body)} chars")
        print(f"anchor text present: {ANCHOR.lower() in body.lower()}")
        print(f"link present:        {f'href=\"{TARGET}\"' in body}")
        idx = body.lower().find(ANCHOR.lower())
        if idx >= 0:
            print(f"\ncontext: ...{body[max(0, idx - 200):idx + 200]}...")
    finally:
        await cms.aclose()


asyncio.run(main())
