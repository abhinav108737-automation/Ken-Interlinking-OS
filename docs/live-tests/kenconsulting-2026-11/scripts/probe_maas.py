# -*- coding: utf-8 -*-
import asyncio, html, re, httpx
from ken_core.config import get_settings

SLUG = "united-arab-emirates-mobility-as-a-service-platforms-maas-market"
WORDS = ["fleet", "taxi", "vehicles", "rental", "charging", "leasing"]
SKIP = {"v02_table_of_contents", "v02_research_methodology"}

def strip(m):
    return html.unescape(re.sub(r"<[^>]+>", "", m))

async def main():
    s = get_settings()
    heads = {"Authorization": f"Bearer {s.cms_read_token.get_secret_value()}"}
    async with httpx.AsyncClient(base_url=(s.cms_base_url or "").rstrip("/"), timeout=120.0, trust_env=False) as c:
        rep = (await c.get(f"/internal/reports/{SLUG}/", headers=heads)).json()
        seen = 0
        for field in rep.get("fields", []):
            name, body = field.get("field"), field.get("body_html") or ""
            if name in SKIP or not body:
                continue
            for pm in re.finditer(r"<p\b[^>]*>(.*?)</p>", body, re.S | re.I):
                chunk = strip(pm.group(1)).strip()
                if len(chunk) < 150 or "\t" in chunk:
                    continue
                for w in WORDS:
                    m = re.search(re.escape(w), chunk, re.I)
                    if m and seen < 8:
                        seen += 1
                        print(f"{name} | '{chunk[m.start():m.end()]}'")
                        print(f"   ...{chunk[max(0,m.start()-100):m.end()+100]}...\n")
                        break

asyncio.run(main())
