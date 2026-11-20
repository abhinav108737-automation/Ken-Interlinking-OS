"""Read-only validation probe for Snehal's Ken Consulting content-change contract.

The payload shape comes from the 2026-11-19 handover. This script performs a dry run only;
it never calls the live apply endpoint and never prints credentials or complete page content.
"""
from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit

import httpx

from ken_core.config import get_settings

SLUG = "kuwait-cloud-based-procurement-spend-control-platforms-market"
FIELD = "v02_growth_drivers"
PAGE_ID = f"{SLUG}:{FIELD}"
FIND = "create recurring data-maintenance needs"
HREF = "/industry-reports/kuwait-cloud-based-document-management-systems-market"
NEW_TEXT = (
    "create recurring "
    f'<a href="{HREF}">document management</a> and data-maintenance needs'
)


async def main() -> None:
    settings = get_settings()
    base = str(settings.cms_base_url or "").rstrip("/")
    token = settings.cms_write_token
    parts = urlsplit(base)
    if parts.hostname not in {"www.thekenconsulting.com", "thekenconsulting.com"}:
        raise RuntimeError("Refusing to probe an unexpected CMS host")
    if token is None or not token.get_secret_value():
        raise RuntimeError("CMS write token is not configured")
    headers = {
        "Accept": "application/json",
        "Authorization": "Bearer " + token.get_secret_value(),
        "Content-Type": "application/json",
    }
    page_path = f"/internal/pages/{PAGE_ID}/"
    validate_path = page_path + "content-changes/validate/"
    async with httpx.AsyncClient(
        base_url=base,
        headers=headers,
        follow_redirects=False,
        timeout=20,
        trust_env=False,
    ) as client:
        page_response = await client.get(page_path)
        page = page_response.json()
        payload = {
            "expected_version": page["version"],
            "change": {"op": "replace", "find": FIND, "new_text": NEW_TEXT},
        }
        response = await client.post(validate_path, content=json.dumps(payload))
        try:
            data = response.json()
        except ValueError:
            data = {"error": "non-JSON response"}
        print(
            json.dumps(
                {
                    "status_code": response.status_code,
                    "valid": data.get("valid") if isinstance(data, dict) else None,
                    "response_keys": sorted(data) if isinstance(data, dict) else [],
                    "find_occurrences": str(page.get("body_html", "")).count(FIND),
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
