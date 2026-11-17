"""Read-only: list every report in the Ken Consulting CMS with its live URL."""
from __future__ import annotations

import asyncio
import json

import httpx

from ken_core.config import get_settings


async def main() -> None:
    s = get_settings()
    base = (s.cms_base_url or "").rstrip("/")
    token = s.cms_read_token.get_secret_value() if s.cms_read_token else ""
    rows, cursor = [], None
    async with httpx.AsyncClient(base_url=base, timeout=120.0, trust_env=False) as c:
        while True:
            path = "/internal/pages/?limit=200" + (f"&after={cursor}" if cursor else "")
            r = await c.get(path, headers={"Authorization": f"Bearer {token}"})
            r.raise_for_status()
            data = r.json()
            rows += data.get("results", [])
            cursor = data.get("next_after")
            if not cursor:
                break
        print(f"{len(rows)} reports in the CMS")
        print("list-endpoint keys:", sorted(rows[0].keys()) if rows else "-")
        out = []
        for row in rows:
            slug = row.get("slug")
            rep = await c.get(
                f"/internal/reports/{slug}/", headers={"Authorization": f"Bearer {token}"}
            )
            d = rep.json() if rep.status_code == 200 else {}
            out.append(
                {
                    "slug": slug,
                    "title": d.get("title") or row.get("title") or "",
                    "url": d.get("url") or "",
                    "protected": bool(d.get("protected", False)),
                    "fields": len(d.get("fields") or []),
                }
            )
    print(json.dumps(out, indent=1)[:200])
    with open("reports.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    print("\n--- all reports ---")
    for i, rrow in enumerate(out, 1):
        print(f"{i:>2}. {rrow['title']}")
        print(f"    {rrow['url']}")
        print(f"    slug={rrow['slug']}  fields={rrow['fields']}  protected={rrow['protected']}")


asyncio.run(main())
