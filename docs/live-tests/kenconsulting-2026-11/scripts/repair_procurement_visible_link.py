"""Move deployment 20 from hidden description copy to a visible growth-driver bullet.

This is a one-page, owner-approved live repair. It is version-locked and compensates back
to the original hidden link if the content edit or replacement link fails deterministically.
Unknown network outcomes stop for reconciliation rather than guessing.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from urllib.parse import urlsplit

import httpx

os.environ["KEN_CMS_WRITES_ENABLED"] = "true"

from ken_core.config import get_settings  # noqa: E402
from ken_core.providers.cms.factory import build_cms_provider  # noqa: E402

SLUG = "kuwait-cloud-based-procurement-spend-control-platforms-market"
FIELD = "v02_growth_drivers"
PAGE_ID = f"{SLUG}:{FIELD}"
TARGET_SLUG = "kuwait-cloud-based-document-management-systems-market"
HREF = f"/industry-reports/{TARGET_SLUG}"
ANCHOR = "document management"
OLD_TEXT = "create recurring data-maintenance needs"
NEW_TEXT = "create recurring document management and data-maintenance needs"
NEW_HTML = (
    "create recurring "
    f'<a href="{HREF}">document management</a> and data-maintenance needs'
)
HIDDEN_DEPLOYMENT_ID = "23"
ACTOR = "abhinav.kumar"

NAMESPACE = uuid.UUID("1631bc83-5d8b-4ca8-bc0c-09a4df46ba9f")
ROLLBACK_OPERATION = uuid.uuid5(NAMESPACE, "rollback-hidden-deployment-20")
RESTORE_LINK_OPERATION = uuid.uuid5(NAMESPACE, "restore-hidden-document-management-link")
RESTORE_LINK_CHANGE = uuid.uuid5(NAMESPACE, "restore-hidden-document-management-change")
CONTENT_OPERATION = "procurement-visible-document-management-copy-v1"


async def _restore_hidden(cms: object) -> str:
    page = await cms.get_page(page_id=PAGE_ID)  # type: ignore[attr-defined]
    if f'href="{HREF}"' in page.body_html:
        return "already-present"
    from ken_contracts.cms import LinkChange, LinkChangeRequest, LinkLocator, LinkOperation

    request = LinkChangeRequest(
        operation_id=RESTORE_LINK_OPERATION,
        source_page_id=PAGE_ID,
        expected_version=page.version,
        changes=[
            LinkChange(
                change_id=RESTORE_LINK_CHANGE,
                operation=LinkOperation.INSERT,
                target_page_id=TARGET_SLUG,
                href=HREF,
                anchor_text=ANCHOR,
                locator=LinkLocator(block_selector=FIELD, occurrence_index=0),
            )
        ],
        dry_run=False,
        actor=ACTOR,
    )
    result = await cms.apply_link_changes(request=request)  # type: ignore[attr-defined]
    return result.deployment_id


async def main() -> None:
    settings = get_settings()
    base = str(settings.cms_base_url or "").rstrip("/")
    token = settings.cms_write_token
    parts = urlsplit(base)
    if parts.hostname not in {"www.thekenconsulting.com", "thekenconsulting.com"}:
        raise RuntimeError("Refusing to write to an unexpected CMS host")
    if token is None or not token.get_secret_value():
        raise RuntimeError("CMS write token is not configured")

    cms = build_cms_provider(settings)
    content_deployment: str | None = None
    try:
        before = await cms.get_page(page_id=PAGE_ID)
        if before.protected:
            raise RuntimeError("Page is protected")
        if before.body_html.count(f'href="{HREF}"') != 1:
            raise RuntimeError("Expected exactly one hidden target link before repair")
        if before.body_html.count(OLD_TEXT) != 1:
            raise RuntimeError("Expected the visible source phrase exactly once")
        if NEW_TEXT in before.body_html:
            raise RuntimeError("Visible copy edit is already present")

        await cms.rollback(
            deployment_id=HIDDEN_DEPLOYMENT_ID,
            operation_id=ROLLBACK_OPERATION,
            actor=ACTOR,
        )
        unlinked = await cms.get_page(page_id=PAGE_ID)
        if f'href="{HREF}"' in unlinked.body_html:
            raise RuntimeError("Deployment 20 rollback did not remove the hidden link")

        headers = {
            "Accept": "application/json",
            "Authorization": "Bearer " + token.get_secret_value(),
            "Content-Type": "application/json",
        }
        change = {
            "op": "replace",
            "find": OLD_TEXT,
            "new_text": NEW_HTML,
        }
        validation_payload = {
            "expected_version": unlinked.version,
            "change": change,
        }
        content_payload = {
            "operation_id": CONTENT_OPERATION,
            "expected_version": unlinked.version,
            "change": change,
        }
        path = f"/internal/pages/{PAGE_ID}/content-changes/"
        async with httpx.AsyncClient(
            base_url=base,
            headers=headers,
            follow_redirects=False,
            timeout=20,
            trust_env=False,
        ) as client:
            validation_response = await client.post(
                path + "validate/", content=json.dumps(validation_payload)
            )
            try:
                validation_data = validation_response.json()
            except ValueError:
                validation_data = {}
            if (
                validation_response.status_code != 200
                or not isinstance(validation_data, dict)
                or validation_data.get("valid") is not True
            ):
                restored = await _restore_hidden(cms)
                raise RuntimeError(
                    f"Content dry run refused with HTTP {validation_response.status_code}; "
                    f"original hidden link restored as deployment {restored}"
                )
            response = await client.post(path, content=json.dumps(content_payload))
        if response.status_code not in {200, 201}:
            restored = await _restore_hidden(cms)
            raise RuntimeError(
                f"Content edit refused with HTTP {response.status_code}; "
                f"original hidden link restored as deployment {restored}"
            )
        data = response.json()
        if not isinstance(data, dict) or data.get("deployment_id") is None:
            raise RuntimeError("Content edit outcome is unknown; reconcile before retrying")
        content_deployment = str(data["deployment_id"])

        after = await cms.get_page(page_id=PAGE_ID)
        linked_fragment = f'<a href="{HREF}">{ANCHOR}</a>'
        if (
            after.body_html.count(linked_fragment) != 1
            or after.body_html.count(NEW_HTML) != 1
            or OLD_TEXT in after.body_html
        ):
            raise RuntimeError("Content edit verification failed; reconcile before retrying")

        print(
            json.dumps(
                {
                    "status": "repaired",
                    "page_id": PAGE_ID,
                    "rolled_back_deployment_id": HIDDEN_DEPLOYMENT_ID,
                    "content_deployment_id": content_deployment,
                    "old_text": OLD_TEXT,
                    "new_text": NEW_TEXT,
                    "new_html": NEW_HTML,
                    "target": HREF,
                    "new_version": after.version,
                },
                indent=2,
            )
        )
    finally:
        await cms.aclose()


if __name__ == "__main__":
    asyncio.run(main())
