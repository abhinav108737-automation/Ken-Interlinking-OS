"""Read-only inspection of the procurement report growth-driver field."""
from __future__ import annotations

import asyncio

from ken_core.config import get_settings
from ken_core.providers.cms.factory import build_cms_provider

PAGE_ID = (
    "kuwait-cloud-based-procurement-spend-control-platforms-market"
    ":v02_growth_drivers"
)
TERMS = ("Published bidder", "Supplier registration", "document management")


async def main() -> None:
    cms = build_cms_provider(get_settings())
    try:
        page = await cms.get_page(page_id=PAGE_ID)
        print(f"version={page.version}; protected={page.protected}; chars={len(page.body_html)}")
        for term in TERMS:
            index = page.body_html.lower().find(term.lower())
            print(f"{term!r}: index={index}")
            if index >= 0:
                print(page.body_html[max(0, index - 180) : index + 420])
    finally:
        await cms.aclose()


if __name__ == "__main__":
    asyncio.run(main())
