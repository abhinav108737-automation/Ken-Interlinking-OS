"""Roll back deployment 6 and restore the clean sentence."""
from __future__ import annotations

import asyncio
import os
import uuid

os.environ["KEN_CMS_WRITES_ENABLED"] = "true"

from ken_core.config import get_settings  # noqa: E402
from ken_core.providers.cms.factory import build_cms_provider  # noqa: E402

PAGE = (
    "united-arab-emirates-regional-auto-components-import-and-distribution-market"
    ":v02_market_overview"
)
TARGET = "/industry-reports/qatar-tire-distribution-and-retail-service-ecosystem-market"


async def main() -> None:
    cms = build_cms_provider(get_settings())
    try:
        before = await cms.get_page(page_id=PAGE)
        print(f"BEFORE    {len(before.body_html)} chars  link={f'href=\"{TARGET}\"' in before.body_html}")
        result = await cms.rollback(
            deployment_id="6", operation_id=uuid.uuid4(), actor="abhinav.kumar"
        )
        print(f"ROLLBACK  status={result.status}")
        after = await cms.get_page(page_id=PAGE)
        print(f"AFTER     {len(after.body_html)} chars  link={f'href=\"{TARGET}\"' in after.body_html}")
        i = after.body_html.find("comparable specifications")
        print(f"\nsentence now: ...{after.body_html[i:i + 200]}...")
    finally:
        await cms.aclose()


asyncio.run(main())
