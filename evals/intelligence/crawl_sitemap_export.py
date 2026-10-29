"""Polite, resumable, read-only fetch of every sitemap page into a v1 JSONL export.

Approved by the owner on 2026-10-29 (ADR-0025): read-only; identified user-agent; ``robots.txt``
honoured (``Disallow`` rules for ``*`` are applied to every planned URL); at most two requests in
flight, each worker waiting at least ``--delay`` seconds after a response; back-off on 429/5xx.

The body is the raw server HTML with ``<script>``, ``<style>`` and ``<svg>`` removed (they carry no
links or text, and the scripts hold megabytes of framework payload). Hidden streaming segments
(``<div hidden id="S:n">``) are kept: the inventory extractor reads them (commit c5aebb5).

    .venv/Scripts/python.exe evals/intelligence/crawl_sitemap_export.py OUT.jsonl [--limit N]

Re-running resumes: URLs already in OUT.jsonl (or in OUT.skipped.jsonl) are not fetched again.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

SITE = "https://www.kenresearch.com"
USER_AGENT = "KenInterlinkBot/0.1 (self-hosted content inventory; read-only)"
SITEMAPS = {
    "home": ["home-sitemap.xml"],
    "static": ["staticpage-sitemap.xml"],
    "brand_comparison": ["brandcomparison-sitemap.xml"],
    "pov": ["pov-sitemap.xml"],
    "case_study": ["casestudy-sitemap.xml"],
    "article": ["article-sitemap.xml"],
    "competition_benchmarking": ["competitionbenchmarking-sitemap.xml"],
    "survey": ["survey-sitemap.xml"],
    "report": ["product-sitemap.xml", "product-sitemap.xml?p=2"],
}
_STRIP = re.compile(r"<(script|style|svg|noscript)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_LANG = re.compile(r"<html[^>]*\blang=[\"']([^\"']+)", re.IGNORECASE)


def _disallow_rules(robots: str) -> list[re.Pattern[str]]:
    """Disallow rules of the ``User-agent: *`` group as regexes (``*`` wildcard, ``$`` end)."""
    rules: list[re.Pattern[str]] = []
    in_star = False
    seen_rule = False
    for raw in robots.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, value = (p.strip() for p in line.split(":", 1))
        field = field.lower()
        if field == "user-agent":
            if seen_rule:
                in_star, seen_rule = False, False
            in_star = in_star or value == "*"
        elif field == "disallow" and in_star:
            seen_rule = True
            if value:
                rules.append(
                    re.compile("^" + re.escape(value).replace(r"\*", ".*").replace(r"\$", "$"))
                )
        elif field == "allow":
            seen_rule = True
    return rules


async def _get(client: httpx.AsyncClient, url: str, retries: int = 3) -> httpx.Response | None:
    for attempt in range(retries):
        try:
            resp = await client.get(url)
        except httpx.HTTPError:
            await asyncio.sleep(2.0 * (attempt + 1))
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            await asyncio.sleep(5.0 * (attempt + 1))
            continue
        return resp
    return None


async def build_plan(client: httpx.AsyncClient, delay: float) -> list[dict[str, str]]:
    robots = await _get(client, f"{SITE}/robots.txt")
    rules = _disallow_rules(robots.text if robots and robots.status_code == 200 else "")
    plan: list[dict[str, str]] = []
    seen: set[str] = set()
    for kind, files in SITEMAPS.items():
        for name in files:
            await asyncio.sleep(delay)
            resp = await _get(client, f"{SITE}/{name}")
            if resp is None or resp.status_code != 200:
                print(f"sitemap {name}: unavailable", file=sys.stderr)
                continue
            for url in re.findall(r"<loc>([^<]+)</loc>", resp.text):
                parsed = urlparse(url)
                path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
                if parsed.netloc != "www.kenresearch.com" or url in seen:
                    continue
                if any(r.match(path) for r in rules) or path.endswith(".md"):
                    continue
                seen.add(url)
                plan.append({"url": url, "kind": kind})
    return sorted(plan, key=lambda p: (p["kind"], p["url"]))


def _record(item: dict[str, str], html: str) -> dict[str, Any]:
    title = _TITLE.search(html)
    lang = _LANG.search(html)
    body = _STRIP.sub("", html)
    return {
        "url": item["url"],
        "body_html": body,
        "title": re.sub(r"\s+", " ", title.group(1)).strip() if title else None,
        "language": lang.group(1) if lang else None,
        "publication_state": "published",
        "metadata": {"sitemap_kind": item["kind"], "fetched_by": "httpx-raw"},
    }


async def main_async(out: Path, limit: int | None, delay: float, workers: int) -> None:
    skipped_path = out.with_suffix(".skipped.jsonl")
    done: set[str] = set()
    for path in (out, skipped_path):
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    done.add(json.loads(line)["url"])
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html"}
    async with httpx.AsyncClient(headers=headers, timeout=30.0, follow_redirects=False) as client:
        plan = await build_plan(client, delay)
        todo = [p for p in plan if p["url"] not in done]
        if limit is not None:
            todo = todo[:limit]
        print(f"plan {len(plan)} urls, already done {len(done)}, fetching {len(todo)}", flush=True)
        queue: asyncio.Queue[dict[str, str]] = asyncio.Queue()
        for item in todo:
            queue.put_nowait(item)
        lock = asyncio.Lock()
        counts = {"ok": 0, "skipped": 0}

        async def worker() -> None:
            while True:
                try:
                    item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                resp = await _get(client, item["url"])
                if (
                    resp is not None
                    and resp.status_code == 200
                    and "text/html" in resp.headers.get("content-type", "")
                ):
                    line, target, key = (
                        json.dumps(_record(item, resp.text), ensure_ascii=False),
                        out,
                        "ok",
                    )
                else:
                    status = resp.status_code if resp is not None else 0
                    line = json.dumps({"url": item["url"], "kind": item["kind"], "status": status})
                    target, key = skipped_path, "skipped"
                async with lock:
                    with target.open("a", encoding="utf-8", newline="\n") as fh:
                        fh.write(line + "\n")
                    counts[key] += 1
                    total = counts["ok"] + counts["skipped"]
                    if total % 100 == 0:
                        print(
                            f"{total}/{len(todo)} ok={counts['ok']} skipped={counts['skipped']}",
                            flush=True,
                        )
                await asyncio.sleep(delay)

        await asyncio.gather(*(worker() for _ in range(workers)))
        print(f"finished ok={counts['ok']} skipped={counts['skipped']}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    asyncio.run(main_async(args.out, args.limit, args.delay, min(args.workers, 2)))


if __name__ == "__main__":
    main()
