"""Stage 15: first-party analytics through the REAL loopback collector.

A browser is not run (nothing here executes ``k.js``); the demo POSTs exactly what the beacon
would send, over a real HTTP connection to the real collector, with the headers a browser
sends. What is exercised for real: origin check, bot filter, live-link resolution, minute
truncation, the pseudonymous sink, aggregation, the ``synthetic`` flag and the measurement sweep.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from typing import Final

import httpx

from ken_contracts.analytics import DateRange
from ken_services.analytics.aggregate import run_aggregation
from ken_services.analytics.policy import load_configured_analytics_policy
from ken_services.demo.context import Ctx, fetchall, scalar
from ken_services.demo.pipeline import path_of

__all__ = ["stage_analytics"]

BROWSER_UA: Final = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15"
)
BOT_UA: Final = "ExampleBot/1.0 (+https://example.invalid/bot)"
FOREIGN_ORIGIN: Final = "https://not-this-site.example.invalid"


async def _post(
    client: httpx.AsyncClient,
    ctx: Ctx,
    *,
    origin: str,
    agent: str,
    path: str,
    events: list[dict[str, str]],
) -> int:
    body = {"v": 1, "pv": secrets.token_hex(16), "path": path, "events": events}
    response = await client.post(
        ctx.collector_url + "/e",
        content=json.dumps(body),
        headers={"Origin": origin, "User-Agent": agent, "Content-Type": "text/plain"},
    )
    return response.status_code


async def stage_analytics(ctx: Ctx) -> None:
    out = ctx.out
    out.stage(15, "Analytics: events through the real loopback collector, aggregation, measurement")
    await ctx.collector_app.index.refresh()
    live = await fetchall(
        ctx,
        "SELECT id, source_url FROM link_plans WHERE status = 'verified' ORDER BY source_url, "
        "target_url",
    )
    out.kv(
        "live links the collector knows (verified plans)",
        f"{getattr(ctx.collector_app.index, 'size', 0)} of {len(live)}",
    )
    out.kv(
        "collector allowed origins (loopback => events flagged synthetic)",
        ", ".join(ctx.collector_app.allowed_origins).replace(ctx.site_url, "<site>"),
    )
    sample = live[:8]
    stored_before = int(await scalar(ctx, "SELECT count(*) FROM link_events") or 0)
    statuses: list[int] = []
    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
        for row in sample:
            events = [{"t": "imp", "l": str(row.id)}] * 1 + [{"t": "clk", "l": str(row.id)}]
            statuses.append(
                await _post(
                    client,
                    ctx,
                    origin=ctx.site_url,
                    agent=BROWSER_UA,
                    path=path_of(row.source_url),
                    events=events,
                )
            )
        accepted_rows = (
            int(await scalar(ctx, "SELECT count(*) FROM link_events") or 0) - stored_before
        )
        foreign = await _post(
            client,
            ctx,
            origin=FOREIGN_ORIGIN,
            agent=BROWSER_UA,
            path=path_of(sample[0].source_url),
            events=[{"t": "imp", "l": str(sample[0].id)}],
        )
        after_foreign = int(await scalar(ctx, "SELECT count(*) FROM link_events") or 0)
        bot = await _post(
            client,
            ctx,
            origin=ctx.site_url,
            agent=BOT_UA,
            path=path_of(sample[0].source_url),
            events=[{"t": "imp", "l": str(sample[0].id)}],
        )
        after_bot = int(await scalar(ctx, "SELECT count(*) FROM link_events") or 0)
    out.kv(
        "page views posted from the site origin", f"{len(sample)} (HTTP {sorted(set(statuses))})"
    )
    out.kv("link_events stored from those page views", accepted_rows)
    out.kv(
        "foreign-origin request",
        f"HTTP {foreign}; rows stored: {after_foreign - stored_before - accepted_rows}",
    )
    out.kv("bot User-Agent request", f"HTTP {bot}; rows stored: {after_bot - after_foreign}")
    counts = ctx.collector_app.counters.counts
    out.kv(
        "collector rejection counters (reason: count only)",
        json.dumps(dict(sorted(counts.items()))),
    )
    shape = await fetchall(
        ctx,
        "SELECT synthetic, event_type AS kind, count(*) AS n FROM link_events GROUP BY 1, 2 ORDER "
        "BY 1, 2",
    )
    out.kv(
        "stored events (synthetic, type, n)",
        ", ".join(f"({r.synthetic}, {r.kind}, {r.n})" for r in shape),
    )
    policy = load_configured_analytics_policy(ctx.settings)
    today = datetime.now(UTC).date()
    outcome = await run_aggregation(
        sessionmaker=ctx.sessionmaker,
        settings=ctx.settings,
        policy=policy,
        date_range=DateRange(start=today, end=today),
    )
    out.kv("aggregation counters", json.dumps(dict(sorted(outcome.counters.items()))))
    daily = await fetchall(
        ctx,
        "SELECT synthetic, source, count(*) AS rows, coalesce(sum(impressions), 0) AS imps, "
        "coalesce(sum(clicks), 0) AS clicks FROM link_metrics_daily GROUP BY 1, 2 ORDER BY 1, 2",
    )
    out.table(
        ["synthetic", "source", "daily rows", "impressions", "clicks"],
        [(r.synthetic, r.source, r.rows, r.imps, r.clicks) for r in daily],
    )
    measured = int(
        await scalar(
            ctx, "SELECT count(*) FROM link_plans WHERE status IN ('measured', 'learning_captured')"
        )
        or 0
    )
    snapshots = int(await scalar(ctx, "SELECT count(*) FROM link_measurements") or 0)
    real_rows = int(
        await scalar(ctx, "SELECT count(*) FROM link_metrics_daily WHERE NOT synthetic") or 0
    )
    out.kv("non-synthetic daily rows (the only coverage a measurement may use)", real_rows)
    out.kv("plans measured / link_measurements rows", f"{measured} / {snapshots}")
    out.kv("plans moved to measured by this aggregation", outcome.plans_measured)
    out.require(
        "7",
        accepted_rows == len(sample) * 2
        and set(statuses) == {204}
        and foreign == 204
        and after_foreign == stored_before + accepted_rows
        and bot == 204
        and after_bot == after_foreign
        and bool(daily)
        and all(r.synthetic for r in daily)
        and real_rows == 0
        and measured == 0
        and snapshots == 0
        and outcome.plans_measured == 0,
        f"{accepted_rows} events stored; foreign-origin and bot requests answered 204 but stored "
        "0; "
        f"{sum(r.rows for r in daily)} daily rows all synthetic; 0 plans measured",
    )
    out.proves(
        "Analytics",
        "collector filters spoofed/bot traffic; synthetic data never reaches 'measured'",
    )
