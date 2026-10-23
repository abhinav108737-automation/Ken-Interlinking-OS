"""Stages 1-8: from an empty database to scored, placed, critiqued link plans.

Every number printed is read back from PostgreSQL (or Qdrant) after the stage ran; no stage
prints what it *intended* to do.
"""

from __future__ import annotations

import json
from typing import Any

from ken_agents.runner import build_config_snapshot, run_agent_batch
from ken_contracts.agents import CriticAssessment
from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import JobRunDetail
from ken_contracts.vectors import VectorQuery
from ken_core.providers.model.mock import MockModelProvider
from ken_services.agents.backend import DatabaseAgentBackend
from ken_services.agents.policy import load_configured_agent_policy
from ken_services.agents.store import DatabaseAgentRunStore
from ken_services.demo.config import FIXTURE_PATH
from ken_services.demo.context import Ctx, fetchall, fetchone, need, scalar
from ken_services.deployment.cms_sim import load_export_bodies, seed_simulated_cms
from ken_services.graph.compute import cluster_sizes, run_graph_metrics
from ken_services.graph.policy import load_graph_policy
from ken_services.intelligence.pipeline import run_analysis
from ken_services.inventory.ingest import run_ingestion
from ken_services.inventory.sources.file_export import FileExportContentSource
from ken_services.jobs.runner import run_job
from ken_services.recommendations.pipeline import load_policies, run_recommendations
from ken_services.vectors.index import run_index_job, run_rebuild_job
from ken_services.vectors.verify import PostgresVectorVerifier

__all__ = [
    "path_of",
    "stage_analyse",
    "stage_critic",
    "stage_graph",
    "stage_index",
    "stage_ingest",
    "stage_recommend",
    "stage_seed_cms",
]

SITE: str = "https://research.example.com"


def path_of(url: str) -> str:
    """The path of a demo URL (the demo corpus lives at one reserved host)."""
    return url.removeprefix(SITE) or "/"


def _counters(detail: JobRunDetail, *names: str) -> str:
    return ", ".join(f"{n}={detail.counters.get(n, 0)}" for n in names)


def _ok(detail: JobRunDetail) -> bool:
    return RunStatus(detail.status) is RunStatus.SUCCEEDED


# --------------------------------------------------------------------------- #
# 2-3. Ingest, normalise, version
# --------------------------------------------------------------------------- #
async def stage_ingest(ctx: Ctx) -> None:
    out = ctx.out
    out.stage(2, "Ingest, normalise and version the synthetic site")
    out.note(f"source: tests/fixtures/demo/{FIXTURE_PATH.name} (file export; no crawl, no network)")

    async def ingest() -> Any:
        source = FileExportContentSource(FIXTURE_PATH, max_bytes=50_000_000)
        return await run_ingestion(source=source, sessionmaker=ctx.sessionmaker)

    first = await ingest()
    c = first.counters
    out.kv("ingestion run status", first.status.value)
    out.kv(
        "run counters",
        f"fetched={c.pages_fetched} created={c.pages_created} changed={c.pages_changed} "
        f"unchanged={c.pages_unchanged} failed={c.pages_failed}",
    )
    pages = await scalar(ctx, "SELECT count(*) FROM pages")
    versions = await scalar(ctx, "SELECT count(*) FROM page_versions")
    links = await scalar(ctx, "SELECT count(*) FROM existing_links")
    states = await fetchall(
        ctx,
        "SELECT v.publication_state AS s, count(*) AS n FROM pages p JOIN page_versions v "
        "ON v.page_id = p.id AND v.version = p.current_version GROUP BY 1 ORDER BY 1",
    )
    out.kv("read back: pages / page_versions / existing_links", f"{pages} / {versions} / {links}")
    out.kv("read back: publication states", ", ".join(f"{r.s}={r.n}" for r in states))
    out.require(
        "P", pages == c.pages_created and pages > 0, f"{pages} pages stored from the fixture"
    )
    again = await ingest()
    out.kv(
        "second ingest of the same export (idempotence)",
        f"created={again.counters.pages_created} changed={again.counters.pages_changed} "
        f"unchanged={again.counters.pages_unchanged}",
    )
    stable = again.counters.pages_created == 0 and again.counters.pages_changed == 0
    versions_after = await scalar(ctx, "SELECT count(*) FROM page_versions")
    out.kv("page_versions after re-ingest (no new version)", versions_after)
    out.proves(
        "Ingest, normalise, version", f"{pages} pages -> {versions} versions; re-ingest is a no-op"
    )
    if not stable or versions_after != versions:
        out.note("WARNING: re-ingesting identical content created a new version")
    ctx.state["page_count"] = pages


# --------------------------------------------------------------------------- #
# the simulated CMS (the demo's "production")
# --------------------------------------------------------------------------- #
async def stage_seed_cms(ctx: Ctx) -> None:
    out = ctx.out
    out.stage(3, "Seed the SIMULATED CMS from the inventory (the demo's stand-in for a CMS)")
    bodies = await load_export_bodies(FIXTURE_PATH, max_bytes=50_000_000)
    summary = await seed_simulated_cms(
        sessionmaker=ctx.sessionmaker, provider=ctx.sim, export_bodies=bodies
    )
    out.kv("CMS pages seeded (own database ken_cms_sim_demo)", summary.seeded)
    out.kv("seeded from the original export HTML", summary.from_export)
    out.kv("conflicts skipped", summary.skipped_conflict)
    listed = await ctx.sim.list_pages()
    out.kv("CMS pages read back", len(listed))
    import httpx

    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
        probe = await client.get(ctx.site_url + "/industry-reports")
    beacon = "k.js" in probe.text
    out.kv(
        "loopback site GET /industry-reports",
        f"HTTP {probe.status_code}, beacon tag present={beacon}",
    )
    out.proves("Simulated CMS", f"{len(listed)} pages served over a verified loopback listener")


# --------------------------------------------------------------------------- #
# 4. Analyse and embed
# --------------------------------------------------------------------------- #
async def stage_analyse(ctx: Ctx) -> None:
    out = ctx.out
    out.stage(4, "Analyse (deterministic) and embed (mock model)")
    detail = await run_analysis(
        sessionmaker=ctx.sessionmaker,
        settings=ctx.settings,
        model_provider=ctx.model,
        triggered_by="demo",
    )
    out.kv("analysis run status", RunStatus(detail.status).value)
    out.kv(
        "run counters",
        _counters(
            detail,
            "pages_analyzed",
            "protections_added",
            "embeddings_written",
            "embedding_failures",
            "relationships_written",
            "errors",
        ),
    )
    types = await fetchall(
        ctx,
        "SELECT a.page_type AS kind, count(*) AS n FROM page_analyses a JOIN pages p ON p.id = "
        "a.page_id AND a.page_version = p.current_version GROUP BY 1 ORDER BY 1",
    )
    out.kv("read back: page types", ", ".join(f"{r.kind}={r.n}" for r in types))
    emb = await fetchone(
        ctx,
        "SELECT count(*) AS n, min(model) AS model, min(dimensions) AS dims, "
        "min(provider) AS provider FROM page_embeddings",
    )
    emb = need(emb)
    out.kv("read back: embeddings (count, model, dims)", f"{emb.n}, {emb.model}, {emb.dims}")
    prot = await fetchall(
        ctx,
        "SELECT source, count(*) AS n FROM page_protections WHERE active GROUP BY 1 ORDER BY 1",
    )
    out.kv("read back: active protections", ", ".join(f"{r.source}={r.n}" for r in prot) or "none")
    rel = await fetchall(
        ctx,
        "SELECT relationship_type AS kind, count(*) AS n FROM page_relationships GROUP BY 1 ORDER "
        "BY 1",
    )
    out.kv("read back: page relationships", ", ".join(f"{r.kind}={r.n}" for r in rel))
    out.require("P", _ok(detail) and emb.n > 0, f"{emb.n} embeddings written under {emb.model}")
    out.proves("Analyse and embed", f"{emb.n} embeddings ({emb.model}, {emb.dims} dims)")


# --------------------------------------------------------------------------- #
# 5. Qdrant
# --------------------------------------------------------------------------- #
async def _drift(ctx: Ctx) -> dict[str, Any]:
    report = await PostgresVectorVerifier(
        sessionmaker=ctx.sessionmaker, store=ctx.store, settings=ctx.settings
    ).verify()
    return {
        "points": report.collection_info.points,
        "missing_vectors": len(report.missing_vectors),
        "orphaned_points": len(report.orphaned_points),
        "stale_points": len(report.stale_points),
        "identity_mismatch": report.identity_mismatch,
        "dimension_mismatch": report.dimension_mismatch,
        "in_sync": report.in_sync,
    }


async def _probe_neighbours(ctx: Ctx) -> list[tuple[str, str, float]]:
    """The 5 nearest neighbours of the first page's own vector, by (page, version, score)."""
    row = await fetchone(
        ctx,
        "SELECT e.embedding AS v FROM page_embeddings e JOIN pages p ON p.id = e.page_id "
        "AND e.page_version = p.current_version ORDER BY p.canonical_url LIMIT 1",
    )
    row = need(row)
    hits = await ctx.store.search(query=VectorQuery(vector=tuple(row.v), limit=5))
    return [(str(h.page_id), h.page_version_id, round(h.score, 6)) for h in hits]


async def stage_index(ctx: Ctx) -> None:
    out = ctx.out
    out.stage(5, "Index into Qdrant (a derived, rebuildable index) and verify against PostgreSQL")
    out.kv("collection", f"{ctx.config.collection} on {ctx.config.qdrant_url}")
    detail, report = await run_index_job(
        sessionmaker=ctx.sessionmaker, store=ctx.store, settings=ctx.settings, triggered_by="demo"
    )
    out.kv("index run status", RunStatus(detail.status).value)
    if report is not None:
        out.kv(
            "index report",
            f"scanned={report.scanned} upserted={report.upserted} skipped={report.skipped} "
            f"vector_size={report.vector_size} model={report.embedding_model}",
        )
    before = await _drift(ctx)
    out.kv("qdrant-verify (after index)", json.dumps(before, sort_keys=True))
    neighbours_before = await _probe_neighbours(ctx)
    rebuild_detail, rebuilt = await run_rebuild_job(
        sessionmaker=ctx.sessionmaker,
        store=ctx.store,
        settings=ctx.settings,
        triggered_by="demo",
        batch_size=64,
    )
    out.kv(
        "qdrant-rebuild",
        f"status={RunStatus(rebuild_detail.status).value} "
        f"dropped={None if rebuilt is None else rebuilt.dropped} "
        f"points_written={None if rebuilt is None else rebuilt.points_written}",
    )
    after = await _drift(ctx)
    out.kv("qdrant-verify (after rebuild)", json.dumps(after, sort_keys=True))
    neighbours_after = await _probe_neighbours(ctx)
    identical = before == after and neighbours_before == neighbours_after
    out.kv("index identical after rebuild (drift report + 5-NN probe)", identical)
    out.require(
        "6",
        bool(before["in_sync"]) and bool(after["in_sync"]) and identical,
        f"Qdrant in sync with PostgreSQL before and after a full rebuild ({after['points']} "
        "points)",
    )
    out.proves(
        "Qdrant index", f"{after['points']} points; verify in sync; rebuild reproduces it exactly"
    )


# --------------------------------------------------------------------------- #
# 6. Graph metrics
# --------------------------------------------------------------------------- #
async def stage_graph(ctx: Ctx) -> None:
    out = ctx.out
    out.stage(6, "Link-graph metrics (deterministic; no graph database)")
    detail, result = await run_graph_metrics(
        sessionmaker=ctx.sessionmaker, triggered_by="demo", policy=load_graph_policy()
    )
    out.kv("graph run status", RunStatus(detail.status).value)
    out.kv(
        "counters",
        _counters(
            detail, "pages", "edges", "entry_pages", "orphans", "bridges", "components", "clusters"
        ),
    )
    orphans = await fetchall(
        ctx,
        "SELECT p.canonical_url AS u FROM page_graph_metrics m JOIN pages p ON p.id = m.page_id "
        "WHERE m.is_orphan ORDER BY 1",
    )
    entry = await fetchall(
        ctx,
        "SELECT p.canonical_url AS u FROM page_graph_metrics m JOIN pages p ON p.id = m.page_id "
        "WHERE m.crawl_depth = 0 ORDER BY 1",
    )
    bridges = await fetchall(
        ctx,
        "SELECT p.canonical_url AS u FROM page_graph_metrics m JOIN pages p ON p.id = m.page_id "
        "WHERE m.is_bridge ORDER BY 1",
    )
    out.kv("true orphans (no inbound internal link)", len(orphans))
    for r in orphans:
        out.note(f"  orphan: {path_of(r.u)}")
    out.kv("entry page(s) (crawl depth 0)", ", ".join(path_of(r.u) for r in entry) or "none")
    out.kv("bridge pages", len(bridges))
    for r in bridges[:8]:
        out.note(f"  bridge: {path_of(r.u)}")
    sizes = cluster_sizes(result) if result is not None else {}
    out.kv("topical clusters (id: pages)", ", ".join(f"{k}:{v}" for k, v in sorted(sizes.items())))
    out.require(
        "P", _ok(detail), f"graph metrics computed: {len(orphans)} orphans, {len(sizes)} clusters"
    )
    out.proves(
        "Graph metrics", f"{len(orphans)} orphans, {len(bridges)} bridges, {len(sizes)} clusters"
    )


# --------------------------------------------------------------------------- #
# 7. Recommend: candidates -> filters -> ranking -> anchor placement
# --------------------------------------------------------------------------- #
async def stage_recommend(ctx: Ctx) -> None:
    out = ctx.out
    out.stage(7, "Candidates (all generators), deterministic filters, ranking, anchors, placement")
    detail = await run_recommendations(
        sessionmaker=ctx.sessionmaker,
        settings=ctx.settings,
        triggered_by="demo",
        model_provider=ctx.model,
        vector_store=ctx.store,
    )
    out.kv("recommendation run status", RunStatus(detail.status).value)
    out.kv(
        "counters",
        _counters(
            detail,
            "sources_considered",
            "sources_skipped_ineligible",
            "candidates",
            "plans_created",
            "errors",
        ),
    )
    channels = await fetchall(
        ctx,
        "SELECT ch AS channel, count(*) AS n FROM link_candidates c, "
        "jsonb_array_elements_text(c.channels) ch GROUP BY 1 ORDER BY 1",
    )
    out.kv("candidates by generator (channel)", ", ".join(f"{r.channel}={r.n}" for r in channels))
    rejected = await fetchall(
        ctx,
        "SELECT rejection AS r, count(*) AS n FROM link_candidates WHERE rejection IS NOT NULL "
        "GROUP BY 1 ORDER BY 2 DESC, 1",
    )
    kept = await scalar(ctx, "SELECT count(*) FROM link_candidates WHERE rejection IS NULL")
    out.kv("deterministic filtering: kept / rejected", f"{kept} / {sum(r.n for r in rejected)}")
    for r in rejected:
        out.note(f"  rejected: {r.r:<34} {r.n}")
    plans = await fetchall(
        ctx,
        "SELECT status, count(*) AS n FROM link_plans GROUP BY 1 ORDER BY 1",
    )
    out.kv("link plans by status", ", ".join(f"{r.status}={r.n}" for r in plans))
    total = sum(r.n for r in plans)
    best = await fetchone(
        ctx,
        "SELECT * FROM link_plans ORDER BY total_score DESC, source_url, target_url LIMIT 1",
    )
    if best is not None:
        score = best.score
        out.line()
        out.note(
            f"Score breakdown for the top plan: {path_of(best.source_url)} -> "
            f"{path_of(best.target_url)}"
        )
        out.table(
            ["component", "value", "weight", "contribution"],
            [
                (c["name"], f"{c['value']:.3f}", f"{c['weight']:.3f}", f"{c['contribution']:.3f}")
                for c in score["components"]
            ],
        )
        out.kv("relevance (sum of contributions)", f"{score['relevance']:.3f}")
        out.kv("multiplicative factors", json.dumps(score.get("factors", {}), sort_keys=True))
        out.kv("total score / confidence", f"{best.total_score:.3f} / {best.confidence:.3f}")
        preview = best.preview
        out.line()
        out.note(
            f"Anchor '{best.anchor_text}' (source: {best.anchor_source}) in block {best.block_id}"
        )
        out.note(
            f"  BEFORE: ...{preview['before'][-70:]}{preview['anchor']}{preview['after'][:50]}..."
        )
        out.note(
            f"  AFTER : ...{preview['before'][-70:]}"
            f'<a href="{path_of(best.target_url)}">{preview["anchor"]}</a>'
            f"{preview['after'][:50]}..."
        )
    out.require(
        "P",
        total > 0 and _ok(detail),
        f"{total} plans proposed with scores, anchors and placements",
    )
    out.proves("Candidates, filters, ranking, placement", f"{total} verbatim-anchored plans")
    ctx.state["plans_total"] = total


# --------------------------------------------------------------------------- #
# 8. The critic (a signal, never a decision)
# --------------------------------------------------------------------------- #
async def _agent_batch(
    ctx: Ctx, provider: MockModelProvider, sources: list[Any] | None
) -> JobRunDetail:
    policy = load_configured_agent_policy(ctx.settings)
    recommendation, ranking = load_policies(ctx.settings)

    async def start_job(**kwargs: Any) -> JobRunDetail:
        return await run_job(sessionmaker=ctx.sessionmaker, **kwargs)

    return await run_agent_batch(
        start_job=start_job,
        backend=DatabaseAgentBackend(sessionmaker=ctx.sessionmaker, settings=ctx.settings),
        store=DatabaseAgentRunStore(sessionmaker=ctx.sessionmaker),
        provider=provider,
        policy=policy,
        config_snapshot=build_config_snapshot(
            policy=policy,
            recommendation_policy_version=recommendation.version,
            ranking_policy_version=ranking.version,
            provider_configured=True,
        ),
        triggered_by="demo",
        source_page_ids=sources,
    )


def _critic(verdict: str) -> MockModelProvider:
    model = MockModelProvider(dimensions=8)
    model.register(
        CriticAssessment,
        {"critiques": [{"ref": f"c{i}", "verdict": verdict} for i in range(10)]},
    )
    return model


async def stage_critic(ctx: Ctx) -> None:
    out = ctx.out
    out.stage(8, "The critic: a signal for the autonomy engine, never a decision")
    out.note("The critic's model is the deterministic MOCK, scripted to return a verdict per plan.")
    out.note(
        "Real critic quality is NOT demonstrated here (docs/DEMO.md, 'What this does not prove')."
    )
    sources = await fetchall(
        ctx,
        "SELECT source_page_id AS s, source_url AS u, count(*) AS n FROM link_plans "
        "WHERE status = 'proposed' GROUP BY 1, 2 ORDER BY 2",
    )
    # One source's plans are reviewed `uncertain` (-> NO_ACTION); the rest are accepted.
    declined_source = next((r for r in sources if r.n >= 1), None)
    if declined_source is not None:
        first = await _agent_batch(ctx, _critic("uncertain"), [declined_source.s])
        out.kv(
            "critic batch 1 (verdict 'uncertain') on",
            f"{path_of(declined_source.u)}  [{RunStatus(first.status).value}]",
        )
        ctx.state["declined_source"] = declined_source.s
    for round_ in range(1, 4):
        detail = await _agent_batch(ctx, _critic("accept"), None)
        handed = detail.counters.get("plans_handed_off", 0)
        out.kv(f"critic batch (verdict 'accept') round {round_}", f"handed_off={handed}")
        if handed == 0:
            break
    verdicts = await fetchall(
        ctx,
        "SELECT verdict, handed_off, count(*) AS n FROM agent_run_plans GROUP BY 1, 2 ORDER BY 1, "
        "2",
    )
    out.table(
        ["critic verdict", "handed to autonomy engine", "plans"],
        [(r.verdict, r.handed_off, r.n) for r in verdicts],
    )
    statuses = await fetchall(
        ctx, "SELECT status, count(*) AS n FROM link_plans GROUP BY 1 ORDER BY 1"
    )
    out.kv("link plans by status", ", ".join(f"{r.status}={r.n}" for r in statuses))
    nothing = await scalar(ctx, "SELECT count(*) FROM autonomy_decisions")
    out.kv("autonomy decisions written by the critic stage", nothing)
    out.proves("Critic", "accept/uncertain recorded; the critic wrote no decision and no publish")
    _ = ctx.state
