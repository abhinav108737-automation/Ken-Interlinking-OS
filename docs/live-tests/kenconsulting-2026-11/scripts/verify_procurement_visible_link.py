"""Revalidate and verify the repaired procurement link in public rendered HTML."""
from __future__ import annotations

import asyncio
from html.parser import HTMLParser

import httpx

SLUG = "kuwait-cloud-based-procurement-spend-control-platforms-market"
PUBLIC_URL = f"https://www.thekenconsulting.com/industry-reports/{SLUG}"
REVALIDATE_URL = "https://www.thekenconsulting.com/api/revalidate"
HREF = "/industry-reports/kuwait-cloud-based-document-management-systems-market"
ANCHOR = "document management"
VISIBLE_CONTEXT = "create recurring document management and data-maintenance needs"


class AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._href: str | None = None
        self._text: list[str] = []
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href is not None:
            self.links.append((self._href, "".join(self._text).strip()))
            self._href = None
            self._text = []


async def main() -> None:
    async with httpx.AsyncClient(follow_redirects=False, timeout=60, trust_env=False) as client:
        for _ in range(6):
            response = await client.post(
                REVALIDATE_URL, json={"slug": SLUG, "type": "v0.2"}
            )
            if response.status_code not in {200, 201}:
                raise RuntimeError(f"Revalidation failed with HTTP {response.status_code}")
        page = await client.get(PUBLIC_URL, headers={"Cache-Control": "no-cache"})
    if page.status_code != 200:
        raise RuntimeError(f"Public page answered HTTP {page.status_code}")
    parser = AnchorParser()
    parser.feed(page.text)
    matches = [link for link in parser.links if link == (HREF, ANCHOR)]
    print(f"public_status={page.status_code}")
    print(f"rendered_anchor_matches={len(matches)}")
    print(f"visible_context_in_html={VISIBLE_CONTEXT in page.text}")
    print(f"target_href_occurrences={page.text.count(HREF)}")
    if len(matches) != 1:
        raise RuntimeError("Expected exactly one rendered public document-management anchor")


if __name__ == "__main__":
    asyncio.run(main())
