"""The analysis job's semantic step through a vector store (ADR-0026), on the test database.

The contract: for the same corpus the store path writes the SAME semantic edges as the exact scan
over PostgreSQL (``test_analysis_pipeline.py`` pins the exact path). Qdrant is not needed: the
in-memory store is exact cosine, which is the right oracle for "the wiring is equivalent". The
Qdrant implementation's own recall is measured against real data in ADR-0026.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.inventory import RunStatus
from ken_contracts.vectors import UpsertResult, VectorPoint, VectorStoreUnavailableError
from ken_core.providers.vectors.memory import MemoryVectorStore
from tests.conftest import make_settings
from tests.integration.intelligence_support import analyse, edges, page, scalar, seed

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

SM = async_sessionmaker[AsyncSession]
SEMANTIC = "semantic_similar"

A = page("/reports/a", "alpha alpha alpha beta market:ev geo:india")
B = page("/reports/b", "alpha alpha beta beta market:ev geo:india")
C = page("/reports/c", "gamma gamma gamma delta market:batteries")


async def test_first_run_through_the_store_matches_the_exact_path(sessionmaker: SM) -> None:
    store = MemoryVectorStore(collection="analysis-test")
    await seed(sessionmaker, A, B, C)
    detail = await analyse(sessionmaker, vector_store=store)
    assert detail.status is RunStatus.SUCCEEDED, detail.counters
    # Exactly what test_first_run_analyses_embeds_and_relates pins for the exact path.
    assert await edges(sessionmaker, SEMANTIC) == {
        ("/reports/a", "/reports/b", SEMANTIC),
        ("/reports/b", "/reports/a", SEMANTIC),
    }
    # The job indexed the store itself: it holds one point per page.
    assert (await store.collection_info()).points == 3


async def test_a_new_page_replaces_an_old_neighbour_through_the_store(sessionmaker: SM) -> None:
    store = MemoryVectorStore(collection="analysis-test")
    settings = make_settings(similarity_top_k=1, similarity_min_score=0.3, embedding_batch_size=4)
    await seed(
        sessionmaker,
        page("/reports/a", "alpha alpha alpha beta"),
        page("/reports/b", "alpha beta beta gamma"),
    )
    await analyse(sessionmaker, settings=settings, vector_store=store)
    assert await edges(sessionmaker, SEMANTIC) == {
        ("/reports/a", "/reports/b", SEMANTIC),
        ("/reports/b", "/reports/a", SEMANTIC),
    }
    # D is nearly identical to A, so it must REPLACE B as A's single neighbour although A itself
    # was not analysed in this run (the incremental "whose top-k does this page enter" probe).
    await seed(sessionmaker, page("/reports/d", "alpha alpha alpha beta beta"))
    detail = await analyse(sessionmaker, settings=settings, vector_store=store)
    assert detail.counters["pages_analyzed"] == 1
    semantic = await edges(sessionmaker, SEMANTIC)
    assert ("/reports/a", "/reports/d", SEMANTIC) in semantic
    assert ("/reports/a", "/reports/b", SEMANTIC) not in semantic
    assert {(s, k) for s, _, k in semantic} == {
        ("/reports/a", SEMANTIC),
        ("/reports/b", SEMANTIC),
        ("/reports/d", SEMANTIC),
    }


async def test_a_rerun_through_the_store_changes_nothing(sessionmaker: SM) -> None:
    store = MemoryVectorStore(collection="analysis-test")
    await seed(sessionmaker, A, B, C)
    await analyse(sessionmaker, vector_store=store)
    before = await edges(sessionmaker)
    count = await scalar(sessionmaker, "SELECT count(*) FROM page_relationships")
    again = await analyse(sessionmaker, vector_store=store)
    assert again.status is RunStatus.SUCCEEDED
    assert await edges(sessionmaker) == before
    assert await scalar(sessionmaker, "SELECT count(*) FROM page_relationships") == count


class _UnavailableStore(MemoryVectorStore):
    async def upsert(self, *, points: Sequence[VectorPoint]) -> UpsertResult:
        raise VectorStoreUnavailableError("simulated outage")


async def test_an_unavailable_store_costs_only_the_semantic_edges(sessionmaker: SM) -> None:
    await seed(sessionmaker, A, B, C)
    detail = await analyse(sessionmaker, vector_store=_UnavailableStore(collection="down"))
    # Counted, not fatal: the page analyses and the taxonomy edges are still written.
    assert detail.counters["errors"] == 1
    assert await edges(sessionmaker, SEMANTIC) == set()
    assert await scalar(sessionmaker, "SELECT count(*) FROM page_analyses") == 3
    assert len(await edges(sessionmaker)) > 0
