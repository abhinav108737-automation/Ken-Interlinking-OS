"""Database flow for the Phase 3 recommendation evals: ingest -> analyse -> recommend -> read.

Used by ``tests/integration/test_recommendation_evals.py`` and
``evals/recommendations/benchmark.py``. It runs ONLY against a database whose name ends in
``_test`` (:func:`require_test_database`): it truncates tables.

The flow is the production one, end to end::

    research_site.jsonl --FileExportContentSource--> run_ingestion
        --run_analysis (MockModelProvider by default)--> analyses, embeddings, relationships
        --run_recommendations--> link_plans / link_candidates / opportunities

and the records are read back with plain SQL into the pure ``judgments`` dataclasses.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.inventory import IngestionRunDetail, RunStatus
from ken_contracts.jobs import JobRunDetail
from ken_contracts.model import ModelProvider
from ken_contracts.recommendations import RankingPolicy, RecommendationPolicy
from tests.unit.recommendation_evals.judgments import PageState, PlanRecord

MOCK_SIMILARITY_MIN_SCORE: Final = 0.3
"""Semantic floor for the MOCK embedder (feature hashing): its cosines are much lower than
nomic's, so the production default (0.78, calibrated for nomic-embed-text) would switch the
semantic relationship and component off. 0.3 is the task's setting for the mock."""

TRUNCATE_RECOMMENDATIONS: Final = (
    "TRUNCATE link_plan_transitions, link_candidates, opportunities, link_plans "
    "RESTART IDENTITY CASCADE"
)
TRUNCATE_EVERYTHING: Final = (
    "TRUNCATE link_plan_transitions, link_candidates, opportunities, link_plans, "
    "page_relationships, page_protections, page_embeddings, page_analyses, job_runs, "
    "existing_links, known_urls, page_versions, pages, ingestion_issues, ingestion_runs, "
    "events RESTART IDENTITY CASCADE"
)
"""TRUNCATE (not DELETE): the append-only / immutability triggers are row triggers."""


def require_test_database(url: str) -> None:
    """Refuse any database whose name does not end in ``_test`` (it truncates tables)."""
    database = urlsplit(url).path.lstrip("/")
    if not database.endswith("_test"):
        raise SystemExit(f"refusing database {database!r}: the name must end in '_test'")


async def execute(sessionmaker: async_sessionmaker[AsyncSession], sql: str) -> None:
    async with sessionmaker() as session, session.begin():
        await session.execute(text(sql))


async def ingest_corpus(
    sessionmaker: async_sessionmaker[AsyncSession], path: Path
) -> IngestionRunDetail:
    from ken_services.inventory.ingest import run_ingestion
    from ken_services.inventory.sources.file_export import FileExportContentSource

    source = FileExportContentSource(path, max_bytes=20_000_000)
    detail = await run_ingestion(source=source, sessionmaker=sessionmaker)
    if detail.status is RunStatus.FAILED or detail.counters.pages_failed:
        raise AssertionError(f"ingestion failed: {detail.status} {detail.counters}")
    return detail


async def analyse(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Any, provider: ModelProvider
) -> JobRunDetail:
    from ken_services.intelligence.pipeline import run_analysis

    return await run_analysis(
        sessionmaker=sessionmaker,
        settings=settings,
        model_provider=provider,
        triggered_by="eval:recommendations",
    )


async def recommend(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Any,
    provider: ModelProvider | None,
    *,
    policy: RecommendationPolicy | None = None,
    ranking: RankingPolicy | None = None,
) -> JobRunDetail:
    from ken_services.recommendations.pipeline import run_recommendations

    return await run_recommendations(
        sessionmaker=sessionmaker,
        settings=settings,
        triggered_by="eval:recommendations",
        policy=policy,
        ranking=ranking,
        model_provider=provider,
    )


# --------------------------------------------------------------------------- #
# Read-back
# --------------------------------------------------------------------------- #
def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def load_plans(sessionmaker: async_sessionmaker[AsyncSession]) -> list[PlanRecord]:
    sql = (
        "SELECT id, run_id, status, source_page_id, source_url, source_page_version, "
        "target_page_id, target_url, target_page_version, anchor_text, anchor_source, block_id, "
        "block_kind, occurrence_index, start_offset, end_offset, context_hash, total_score, "
        "confidence, calibration_version, score, relationship_type, channels "
        "FROM link_plans ORDER BY source_url, total_score DESC, target_url"
    )
    async with sessionmaker() as session:
        rows = (await session.execute(text(sql))).mappings().all()
    plans: list[PlanRecord] = []
    for row in rows:
        score = _json(row["score"]) or {}
        plans.append(
            PlanRecord(
                id=str(row["id"]),
                run_id=None if row["run_id"] is None else str(row["run_id"]),
                status=str(row["status"]),
                source_page_id=str(row["source_page_id"]),
                source_url=str(row["source_url"]),
                source_page_version=int(row["source_page_version"]),
                target_page_id=str(row["target_page_id"]),
                target_url=str(row["target_url"]),
                target_page_version=int(row["target_page_version"]),
                anchor_text=str(row["anchor_text"]),
                anchor_source=str(row["anchor_source"]),
                block_id=str(row["block_id"]),
                block_kind=str(row["block_kind"]),
                occurrence_index=int(row["occurrence_index"]),
                start=int(row["start_offset"]),
                end=int(row["end_offset"]),
                context_hash=str(row["context_hash"]),
                total_score=float(row["total_score"]),
                confidence=float(row["confidence"]),
                calibration_version=row["calibration_version"],
                relevance=float(score.get("relevance", float("nan"))),
                factors={str(k): float(v) for k, v in (score.get("factors") or {}).items()},
                components={
                    str(c["name"]): float(c["contribution"]) for c in score.get("components", ())
                },
                relationship_type=row["relationship_type"],
                channels=tuple(str(c) for c in (_json(row["channels"]) or ())),
            )
        )
    return plans


async def load_pages(sessionmaker: async_sessionmaker[AsyncSession]) -> dict[str, PageState]:
    """Current state of every page, keyed by page id (str)."""
    async with sessionmaker() as session:
        pages = (
            (
                await session.execute(
                    text(
                        "SELECT p.id, p.canonical_url, p.current_version, p.word_count, "
                        "p.gone_at IS NOT NULL AS gone, v.body_html "
                        "FROM pages p JOIN page_versions v "
                        "ON v.page_id = p.id AND v.version = p.current_version"
                    )
                )
            )
            .mappings()
            .all()
        )
        protected = {
            str(r[0])
            for r in (
                await session.execute(
                    text("SELECT DISTINCT page_id FROM page_protections WHERE active")
                )
            ).all()
        }
        markets = {
            str(r[0]): r[1]
            for r in (
                await session.execute(
                    text(
                        "SELECT a.page_id, a.market_id FROM page_analyses a JOIN pages p "
                        "ON p.id = a.page_id AND a.page_version = p.current_version"
                    )
                )
            ).all()
        }
        links = (
            (
                await session.execute(
                    text(
                        "SELECT l.page_id, l.block_id, l.anchor_text, l.link_type, "
                        "coalesce(k.page_id, q.id) AS target "
                        "FROM existing_links l JOIN pages p "
                        "ON p.id = l.page_id AND l.page_version = p.current_version "
                        "LEFT JOIN known_urls k ON k.url = l.resolved_url "
                        "LEFT JOIN pages q ON q.canonical_url = l.resolved_url"
                    )
                )
            )
            .mappings()
            .all()
        )
    linked: dict[str, set[str]] = defaultdict(set)
    internal: dict[str, int] = defaultdict(int)
    by_block: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for link in links:
        page_id = str(link["page_id"])
        by_block[page_id][str(link["block_id"])].append(str(link["anchor_text"]))
        if link["link_type"] == "internal":
            internal[page_id] += 1
            if link["target"] is not None:
                linked[page_id].add(str(link["target"]))
    out: dict[str, PageState] = {}
    for row in pages:
        page_id = str(row["id"])
        out[page_id] = PageState(
            page_id=page_id,
            canonical_url=str(row["canonical_url"]),
            current_version=int(row["current_version"]),
            body_html=str(row["body_html"]),
            word_count=int(row["word_count"]),
            gone=bool(row["gone"]),
            protected=page_id in protected,
            market_id=markets.get(page_id),
            linked_page_ids=frozenset(linked.get(page_id, ())),
            internal_body_links=internal.get(page_id, 0),
            links_by_block={k: tuple(v) for k, v in by_block.get(page_id, {}).items()},
        )
    return out


def target_markets(pages: Mapping[str, PageState]) -> dict[str, str | None]:
    return {page_id: page.market_id for page_id, page in pages.items()}


def page_id_by_url(pages: Mapping[str, PageState]) -> dict[str, uuid.UUID]:
    return {page.canonical_url: uuid.UUID(page.page_id) for page in pages.values()}
