"""Insert the UAE -> Qatar link on the anchor text that is now live, through the ADR-0028 adapter."""
from __future__ import annotations

import asyncio
import os
import uuid

os.environ["KEN_CMS_WRITES_ENABLED"] = "true"

from ken_contracts.cms import (  # noqa: E402
    LinkChange,
    LinkChangeRequest,
    LinkLocator,
    LinkOperation,
)
from ken_core.config import get_settings  # noqa: E402
from ken_core.providers.cms.factory import build_cms_provider  # noqa: E402

SLUG = "united-arab-emirates-regional-auto-components-import-and-distribution-market"
FIELD = "v02_market_overview"
PAGE = f"{SLUG}:{FIELD}"
TARGET_SLUG = "qatar-tire-distribution-and-retail-service-ecosystem-market"
TARGET = f"/industry-reports/{TARGET_SLUG}"
ANCHOR = "Qatar tire distribution market"
ACTOR = "abhinav.kumar"


async def main() -> None:
    cms = build_cms_provider(get_settings())
    try:
        page = await cms.get_page(page_id=PAGE)
        print(f"BEFORE    version {page.version[:12]}  body {len(page.body_html)} chars  "
              f"protected={page.protected}")
        if ANCHOR not in page.body_html:
            print("The anchor text is not in the field; nothing was written.")
            return
        if f'href="{TARGET}"' in page.body_html:
            print("The link is already on the page; nothing to do.")
            return

        change = LinkChange(
            change_id=uuid.uuid4(),
            operation=LinkOperation.INSERT,
            target_page_id=TARGET_SLUG,
            href=TARGET,
            anchor_text=ANCHOR,
            locator=LinkLocator(block_selector=FIELD, occurrence_index=0),
        )
        request = LinkChangeRequest(
            operation_id=uuid.uuid4(),
            source_page_id=PAGE,
            expected_version=page.version,
            changes=[change],
            dry_run=True,
            actor=ACTOR,
        )

        check = await cms.validate_link_changes(request=request)
        print(f"VALIDATE  valid={check.valid}")
        for issue in check.issues:
            print(f"          [{issue.code}] {issue.message}")
        if not check.valid:
            print("Refused at validation; nothing was written.")
            return

        result = await cms.apply_link_changes(
            request=request.model_copy(update={"dry_run": False})
        )
        print(f"APPLY     status={result.status}  deployment_id={result.deployment_id}")
        print(f"          {result.previous_version[:12]} -> {result.new_version[:12]}")

        after = await cms.get_page(page_id=PAGE)
        idx = after.body_html.find(f'href="{TARGET}"')
        print(f"VERIFY    body {len(after.body_html)} chars (was {len(page.body_html)})")
        if idx >= 0:
            print(f"          ...{after.body_html[max(0, idx - 120):idx + 170]}...")
        print(f"\nTO UNDO:  rollback deployment {result.deployment_id}")
    finally:
        await cms.aclose()


asyncio.run(main())
