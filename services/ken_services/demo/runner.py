"""Set-up, orchestration and tear-down of one demo run."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Final

import httpx

from ken_core import logging as ken_logging
from ken_core.config import REPO_ROOT
from ken_core.db.engine import create_engine
from ken_core.db.session import create_readonly_sessionmaker, create_sessionmaker
from ken_core.providers.cms.factory import build_html_engine
from ken_core.providers.cms.simulated import SimulatedCMSProvider, assert_safe_sim_url
from ken_core.providers.model.factory import build_model_provider
from ken_core.providers.vectors.factory import build_vector_store
from ken_services.analytics.collector.app import CollectorApp
from ken_services.analytics.collector.index import SqlLiveLinkIndex
from ken_services.analytics.collector.server import CollectorServer
from ken_services.analytics.collector.sink import SqlLinkEventSink
from ken_services.analytics.policy import load_configured_analytics_policy
from ken_services.analytics.protocol import CollectorConfig
from ken_services.demo import resources
from ken_services.demo.config import (
    FIXTURE_PATH,
    DemoConfig,
    DemoRefusalError,
    settings_for,
)
from ken_services.demo.console import Console
from ken_services.demo.context import Ctx, RecordingCMS, scalar
from ken_services.deployment.sim_site import SimulatedSite

__all__ = ["clean", "open_context", "preflight", "run_demo"]


async def preflight(config: DemoConfig, out: Console) -> None:
    """Refuse early, with a plain reason, when a dependency is missing."""
    problems: list[str] = []
    if not FIXTURE_PATH.is_file():
        problems.append(f"the demo corpus {FIXTURE_PATH.name} is missing")
    try:
        async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
            response = await client.get(config.qdrant_url + "/collections")
            response.raise_for_status()
    except Exception:
        problems.append("Qdrant is not reachable on 127.0.0.1:6333 (run: make qdrant-up)")
    try:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        engine = create_async_engine(
            config.admin_url, poolclass=NullPool, connect_args={"timeout": 3}
        )
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        finally:
            await engine.dispose()
    except Exception:
        problems.append("PostgreSQL is not reachable on 127.0.0.1:5433 (run: make db-up)")
    if problems:
        for problem in problems:
            out.line(f"preflight failed: {problem}")
        raise DemoRefusalError("; ".join(problems))


@asynccontextmanager
async def open_context(config: DemoConfig, out: Console) -> AsyncIterator[Ctx]:
    """Recreate the demo's resources and start its two loopback servers; tear everything
    down on exit (the databases and the collection are left for inspection; ``demo-clean``
    removes them)."""
    ken_logging.configure_logging(level="WARNING", fmt="console")
    await preflight(config, out)
    await resources.recreate_databases(config)
    await resources.migrate(config)
    await resources.delete_collections(config)
    settings = settings_for(config)
    assert_safe_sim_url(settings.cms_sim_database_url)
    engine = create_engine(settings)
    sessionmaker = create_sessionmaker(engine)
    principal, human = await resources.create_identities(sessionmaker)
    model = build_model_provider(settings)
    store = build_vector_store(settings)
    sim = SimulatedCMSProvider(database_url=config.sim_url, html_engine=build_html_engine())
    await sim.ensure_schema()
    await sim.reset()
    policy = load_configured_analytics_policy(settings)
    app = CollectorApp(
        config=CollectorConfig.from_settings(settings, policy),
        index=SqlLiveLinkIndex(create_readonly_sessionmaker(engine)),
        sink=SqlLinkEventSink(sessionmaker),
    )
    collector = CollectorServer(app)
    site: SimulatedSite | None = None
    try:
        collector_url = await collector.start()
        site = SimulatedSite(sim, beacon_src=collector_url)
        site_url = await site.start()
        app.add_allowed_origin(site_url)
        verified = settings_for(config, verify_base_url=site_url)
        out.kv(
            "PostgreSQL (app, simulated CMS)",
            f"{config.app_db}, {config.sim_db} on 127.0.0.1:5433 (recreated)",
        )
        out.kv(
            "Qdrant collection",
            f"{config.collection} on {config.qdrant_url} (recreated)",
        )
        out.kv("simulated CMS site (verified listener)", site_url)
        out.kv("first-party collector (verified listener)", collector_url)
        out.kv(
            "operator identity for the publisher",
            f"{principal.actor} (random password, hashed, never shown)",
        )
        out.kv(
            "human login",
            "demo_admin (from KEN_DEMO_ADMIN_PASSWORD)"
            if human
            else "none (set KEN_DEMO_ADMIN_PASSWORD before `make demo` to create demo_admin)",
        )
        yield Ctx(
            config=config,
            out=out,
            engine=engine,
            sessionmaker=sessionmaker,
            settings=verified,
            model=model,
            store=store,
            sim=sim,
            cms=RecordingCMS(sim),
            site_url=site_url,
            collector_url=collector_url,
            collector_app=app,
            principal=principal,
            human_login=human,
        )
    finally:
        if site is not None:
            await site.stop()
        await collector.stop()
        with contextlib.suppress(Exception):
            await store.aclose()
        with contextlib.suppress(Exception):
            await model.aclose()
        await sim.aclose()
        await engine.dispose()


async def clean(config: DemoConfig) -> list[str]:
    """``make demo-clean``: drop the two demo databases and delete ``ken_demo_*`` collections."""
    await resources.drop_databases(config)
    return await resources.delete_collections(config, every_demo_collection=True)


Stage = Callable[[Ctx], Awaitable[None]]


# --------------------------------------------------------------------------- #
# The whole run
# --------------------------------------------------------------------------- #
BANNER: Final = (
    "KEN INTERLINKING OS - LOCAL END-TO-END DEMONSTRATION",
    "",
    "ALL DATA IS SYNTHETIC: a fictional 64-page research site at the reserved host "
    "research.example.com.",
    "NO real CMS, NO real site, NO Google service and NO network beyond 127.0.0.1 is involved.",
    "The 'CMS' is a simulated one in its own database; the model is a DETERMINISTIC MOCK (no LLM).",
    "Nothing here is evidence about real traffic, real editors or a real model's quality.",
)

NOT_PROVEN: Final = (
    "Real-template HTML parity: the corpus is simplified HTML; a real site's templates, widgets "
    "and "
    "markup quirks are untested.",
    "Real CMS behaviour: the CMS is a simulator we wrote (publish lag, auth, caching, concurrent "
    "editors and API errors of a real CMS are not exercised).",
    "A live model's quality: the critic and embeddings are a deterministic mock; precision of real "
    "verdicts and real semantic retrieval is not shown.",
    "Real traffic: events are POSTed by the demo to the real collector; no browser ran the beacon, "
    "and nothing was measured on real users.",
    "Real review history: calibration and review data are SYNTHETIC fixtures; calibration quality, "
    "learning gains and promotion decisions on real data are not shown.",
)


def _hash_policies() -> str:
    import hashlib

    digest = hashlib.sha256()
    for path in sorted((REPO_ROOT / "policies").rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(REPO_ROOT)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


async def run_demo(config: DemoConfig, out: Console) -> int:
    """Run every stage; print the summary; return the process exit code (0 only when every
    acceptance check passed)."""
    from ken_services.demo import learning, measurement, pipeline, publishing

    out.banner(BANNER)
    out.line()
    out.stage(1, "Disposable resources (recreated for this run; refuses to touch anything else)")
    policies_before = _hash_policies()
    try:
        async with open_context(config, out) as ctx:
            for stage in (
                pipeline.stage_ingest,
                pipeline.stage_seed_cms,
                pipeline.stage_analyse,
                pipeline.stage_index,
                pipeline.stage_graph,
                pipeline.stage_recommend,
                pipeline.stage_critic,
                publishing.stage_modes,
                publishing.stage_block,
                publishing.stage_no_action,
                publishing.stage_rollback,
                publishing.stage_main_pass,
                publishing.stage_calibration,
                measurement.stage_analytics,
                learning.stage_learning,
                learning.stage_governance,
            ):
                await stage(ctx)
            promoted = int(
                await scalar(
                    ctx,
                    "SELECT (SELECT count(*) FROM learning_proposals WHERE status = 'promoted') "
                    "+ (SELECT count(*) FROM skills WHERE status = 'promoted')",
                )
                or 0
            )
            out.line()
            out.kv("policies/ directory unchanged by the demo", _hash_policies() == policies_before)
            out.kv("proposals or skills promoted by the demo", promoted)
            out.require(
                "P",
                promoted == 0 and _hash_policies() == policies_before,
                "no policy or skill was promoted and no policy file was written",
            )
    except DemoRefusalError:
        out.line("The demo refused to run (nothing was changed).")
        return 2
    _summary(out)
    return 0 if not out.failed else 1


def _summary(out: Console) -> None:
    out.line()
    out.line("=" * 100)
    out.line(
        "  SUMMARY - what this run proved (every row asserted; the run exits non-zero otherwise)"
    )
    out.line("=" * 100)
    out.table(
        ["#", "acceptance check", "result"],
        [
            (c.key, c.title, {True: "PASS", False: "FAIL", None: "MISSING"}[c.passed])
            for c in out.checks.values()
        ],
    )
    out.line()
    out.table(["stage", "what was demonstrated"], out.proved)
    out.line()
    out.line("  What this does NOT prove:")
    for item in NOT_PROVEN:
        out.line(f"   - {item}")
    out.line()
    if out.failed:
        out.line(f"  RESULT: FAILED ({', '.join(c.key for c in out.failed)})")
    else:
        out.line("  RESULT: all acceptance checks passed")
    out.line(f"  elapsed {out.elapsed():.0f}s")
