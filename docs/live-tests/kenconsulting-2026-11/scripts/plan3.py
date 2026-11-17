# -*- coding: utf-8 -*-
"""Read-only: pick one prose anchor per report pointing at another report."""
from __future__ import annotations

import asyncio
import html
import json
import re

import httpx

from ken_core.config import get_settings

SKIP = {"v02_table_of_contents", "v02_research_methodology"}
PHRASES = {
    "thailand-ev-charging-infrastructure-and-smart-energy-mobility-ecosystem-market":
        ["EV charging infrastructure", "charging infrastructure", "EV charging"],
    "philippines-automotive-lubricants-distribution-market-assessment-outlook-to":
        ["automotive lubricants", "lubricants distribution", "lubricants"],
    "united-arab-emirates-regional-auto-components-import-and-distribution-market":
        ["auto components", "spare parts", "spare-parts"],
    "united-arab-emirates-mobility-as-a-service-platforms-maas-market":
        ["mobility-as-a-service", "ride-hailing", "ride hailing"],
    "qatar-tire-distribution-and-retail-service-ecosystem-market":
        ["tire distribution", "tyre distribution", "replacement tyres", "tyres"],
    "indonesia-commercial-vehicle-leasing-and-fleet-management-market":
        ["commercial vehicle leasing", "fleet management", "leasing"],
    "malaysia-electric-two-wheeler-adoption-ecosystem-market":
        ["electric two-wheeler", "two-wheeler", "two wheeler"],
}


def strip(markup: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", markup))


def prose_chunks(body: str) -> list[str]:
    out = []
    for m in re.finditer(r"<p\b[^>]*>(.*?)</p>", body, re.S | re.I):
        chunk = strip(m.group(1)).strip()
        if len(chunk) > 150 and "\t" not in chunk:
            out.append(chunk)
    return out


async def main() -> None:
    s = get_settings()
    base = (s.cms_base_url or "").rstrip("/")
    token = s.cms_read_token.get_secret_value() if s.cms_read_token else ""
    heads = {"Authorization": f"Bearer {token}"}
    plan = []
    async with httpx.AsyncClient(base_url=base, timeout=120.0, trust_env=False) as c:
        for slug in PHRASES:
            rep = (await c.get(f"/internal/reports/{slug}/", headers=heads)).json()
            chosen = None
            for target, phrases in PHRASES.items():
                if target == slug or chosen:
                    continue
                for field in rep.get("fields", []):
                    name, body = field.get("field"), field.get("body_html") or ""
                    if name in SKIP or not body or chosen:
                        continue
                    plain = strip(body)
                    for phrase in phrases:
                        if re.search(r"<a\b[^>]*>[^<]*" + re.escape(phrase), body, re.I):
                            continue
                        hit = None
                        for chunk in prose_chunks(body):
                            cm = re.search(re.escape(phrase), chunk, re.I)
                            if cm:
                                hit = chunk[cm.start():cm.end()]
                                break
                        if not hit:
                            continue
                        pm = re.search(re.escape(hit), plain)
                        occ = len(re.findall(re.escape(hit), plain[:pm.start()]))
                        chosen = {
                            "source": slug, "field": name, "anchor": hit,
                            "occurrence": occ, "target": target,
                            "context": plain[max(0, pm.start() - 90):pm.end() + 90].strip(),
                        }
                        break
            plan.append(chosen or {"source": slug, "error": "no prose phrase found"})
    json.dump(plan, open("plan.json", "w", encoding="utf-8"), indent=1)
    for p in plan:
        if "error" in p:
            print(f"\n!! {p['source']}\n   {p['error']}")
            continue
        print(f"\nSOURCE {p['source']}")
        print(f"  field    {p['field']}   occurrence {p['occurrence']}")
        print(f"  anchor   \"{p['anchor']}\"  ->  /industry-reports/{p['target']}")
        print(f"  context  {p['context']}")


asyncio.run(main())
