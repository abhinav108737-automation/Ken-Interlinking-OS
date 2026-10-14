"""The analysis job against the ``*_test`` database, with a fake analyser and embedder."""

from __future__ import annotations

import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.events import EventName
from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import JobCompletedPayload, JobKind
from ken_contracts.model import ModelUnavailableError
from ken_core.db.repositories.events import list_events
from ken_services.intelligence.embeddings import current_recipe_fingerprint
from ken_services.intelligence.pipeline import ANALYSIS_COUNTERS
from tests.conftest import make_settings
from tests.integration.intelligence_support import (
    AV,
    EMBED_MODEL,
    FakeAnalyzer,
    FakeEmbedder,
    analyse,
    edges,
    page,
    page_ids,
    rows,
    scalar,
    seed,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

SM = async_sessionmaker[AsyncSession]
SEMANTIC = "semantic_similar"
COARSE = {"same_geography", "regional_parent", "industry_adjacency"}
FINGERPRINT = current_recipe_fingerprint(make_settings())

# Three pages: A and B share a market (ev) and a geography; C is batteries.
# Embeddings: A ~ B (alpha-heavy), C is gamma-heavy.
A = page("/reports/a", "alpha alpha alpha beta market:ev geo:india")
B = page("/reports/b", "alpha alpha beta beta market:ev geo:india")
C = page("/reports/c", "gamma gamma gamma delta market:batteries")


async def test_first_run_analyses_embeds_and_relates(sessionmaker: SM) -> None:
    await seed(sessionmaker, A, B, C)
    detail = await analyse(sessionmaker)
    assert detail.kind is JobKind.ANALYSIS
    assert detail.status is RunStatus.SUCCEEDED, detail.counters
    assert set(detail.counters) == set(ANALYSIS_COUNTERS)
    c = detail.counters
    assert (c["pages_considered"], c["pages_analyzed"], c["pages_fresh_skipped"]) == (3, 3, 0)
    assert (c["embeddings_written"], c["embeddings_fresh_skipped"]) == (3, 0)
    assert c["errors"] == c["embedding_failures"] == 0
    assert c["relationships_written"] == await scalar(
        sessionmaker, "SELECT count(*) FROM page_relationships"
    )

    analyses = await rows(
        sessionmaker, "SELECT analysis_version, run_id FROM page_analyses ORDER BY page_id"
    )
    assert [(av, run) for av, run in analyses] == [(AV, detail.id)] * 3
    embedded = await rows(
        sessionmaker,
        "SELECT model, provider, dimensions, recipe_version, recipe_fingerprint, "
        "vector_dims(embedding) FROM page_embeddings",
    )
    assert set(embedded) == {(EMBED_MODEL, "fake-embedder", 4, 1, FINGERPRINT, 4)}

    # Taxonomy edges in BOTH directions; none across markets. The fake toolkit also
    # proposes same_geography for A <-> B: a coarse type, never materialised.
    assert {e for e in await edges(sessionmaker) if e[2] != SEMANTIC} == {
        ("/reports/a", "/reports/b", "same_market"),
        ("/reports/b", "/reports/a", "same_market"),
    }
    # Semantic: A <-> B are similar, C is not similar to either above 0.5.
    assert await edges(sessionmaker, SEMANTIC) == {
        ("/reports/a", "/reports/b", SEMANTIC),
        ("/reports/b", "/reports/a", SEMANTIC),
    }
    versions = await rows(
        sessionmaker,
        "SELECT source_page_version, target_page_version, analysis_version, run_id "
        "FROM page_relationships",
    )
    assert {tuple(v) for v in versions} == {(1, 1, AV, detail.id)}


async def test_rerun_is_idempotent(sessionmaker: SM) -> None:
    await seed(sessionmaker, A, B, C)
    first = await analyse(sessionmaker)
    before = await rows(sessionmaker, "SELECT id FROM page_relationships ORDER BY id")
    second = await analyse(sessionmaker)
    assert second.status is RunStatus.SUCCEEDED
    c = second.counters
    assert (c["pages_analyzed"], c["pages_fresh_skipped"]) == (0, 3)
    assert (c["embeddings_written"], c["embeddings_fresh_skipped"]) == (0, 3)
    assert c["relationships_written"] == 0
    assert await scalar(sessionmaker, "SELECT count(*) FROM page_analyses") == 3
    assert await scalar(sessionmaker, "SELECT count(*) FROM page_embeddings") == 3
    # Nothing touched: the derived edges are the very same rows.
    assert await rows(sessionmaker, "SELECT id FROM page_relationships ORDER BY id") == before
    assert first.id != second.id


async def test_new_version_is_reanalysed_reembedded_and_reconnected(sessionmaker: SM) -> None:
    await seed(sessionmaker, A, B, C)
    await analyse(sessionmaker)
    # B moves to batteries and becomes gamma-heavy (new content -> version 2).
    await seed(sessionmaker, page("/reports/b", "gamma gamma gamma delta market:batteries"))
    detail = await analyse(sessionmaker)
    c = detail.counters
    assert (c["pages_analyzed"], c["pages_fresh_skipped"]) == (1, 2)
    assert (c["embeddings_written"], c["embeddings_fresh_skipped"]) == (1, 2)
    ids = await page_ids(sessionmaker)
    # The old analysis and embedding are kept (immutable history).
    assert (
        await scalar(
            sessionmaker,
            "SELECT count(*) FROM page_analyses WHERE page_id = :p",
            p=ids["/reports/b"],
        )
        == 2
    )
    assert (
        await scalar(
            sessionmaker,
            "SELECT count(*) FROM page_embeddings WHERE page_id = :p",
            p=ids["/reports/b"],
        )
        == 2
    )
    assert await edges(sessionmaker) == {
        ("/reports/b", "/reports/c", "same_market"),
        ("/reports/c", "/reports/b", "same_market"),
        ("/reports/b", "/reports/c", SEMANTIC),
        ("/reports/c", "/reports/b", SEMANTIC),
    }
    stamped = await rows(
        sessionmaker,
        "SELECT source_page_version, target_page_version FROM page_relationships "
        "WHERE source_page_id = :p",
        p=ids["/reports/b"],
    )
    assert {tuple(r) for r in stamped} == {(2, 1)}


async def test_protections_are_added_and_deactivated_never_deleted(sessionmaker: SM) -> None:
    locked = page("/reports/a", "alpha market:ev", metadata={"lock": "yes"})
    await seed(sessionmaker, locked, B)
    ids = await page_ids(sessionmaker)
    async with sessionmaker() as session, session.begin():
        from sqlalchemy import text

        await session.execute(
            text(
                "INSERT INTO page_protections (page_id, source, rule_id, reason, created_by) "
                "VALUES (:p, 'manual', NULL, 'operator hold', 'operator@example.com')"
            ),
            {"p": ids["/reports/b"]},
        )
    first = await analyse(sessionmaker)
    assert (first.counters["protections_added"], first.counters["protections_deactivated"]) == (
        1,
        0,
    )
    again = await analyse(sessionmaker)
    assert (again.counters["protections_added"], again.counters["protections_deactivated"]) == (
        0,
        0,
    )
    # The lock is removed in a new version: the POLICY row is deactivated, never deleted.
    await seed(sessionmaker, page("/reports/a", "alpha alpha market:ev"), B)
    third = await analyse(sessionmaker)
    assert (third.counters["protections_added"], third.counters["protections_deactivated"]) == (
        0,
        1,
    )
    found = await rows(
        sessionmaker,
        "SELECT page_id, source, rule_id, active, created_by, deactivated_by "
        "FROM page_protections ORDER BY id",
    )
    assert [tuple(r) for r in found] == [
        (ids["/reports/b"], "manual", None, True, "operator@example.com", None),
        (ids["/reports/a"], "policy", "locked", False, "policy", "policy"),
    ]


async def test_unavailable_embedding_model_is_isolated(sessionmaker: SM) -> None:
    await seed(sessionmaker, A, B, C)
    provider = FakeEmbedder()
    provider.fail_with = ModelUnavailableError("connection refused to http://secret-host:1")
    detail = await analyse(sessionmaker, provider)
    assert detail.status is RunStatus.COMPLETED_WITH_ERRORS
    c = detail.counters
    assert (c["pages_analyzed"], c["embeddings_written"], c["embedding_failures"]) == (3, 0, 3)
    assert c["errors"] == 0
    assert len(provider.embed_calls) == 1  # the run stops asking once the model is gone
    assert await scalar(sessionmaker, "SELECT count(*) FROM page_embeddings") == 0
    # Taxonomy edges still exist; no semantic ones could be computed.
    assert await edges(sessionmaker, SEMANTIC) == set()
    assert len(await edges(sessionmaker)) == 2
    assert "secret-host" not in json.dumps(detail.model_dump(mode="json"))

    healed = await analyse(sessionmaker)
    assert healed.status is RunStatus.SUCCEEDED
    assert (healed.counters["pages_analyzed"], healed.counters["embeddings_written"]) == (0, 3)
    assert len(await edges(sessionmaker, SEMANTIC)) == 2


async def test_a_failing_batch_costs_only_that_batch(sessionmaker: SM) -> None:
    await seed(sessionmaker, A, B, C)
    provider = FakeEmbedder()
    provider.fail_on_calls = {1}  # batch size 2: the first batch times out
    detail = await analyse(sessionmaker, provider)
    assert detail.status is RunStatus.COMPLETED_WITH_ERRORS
    assert (detail.counters["embeddings_written"], detail.counters["embedding_failures"]) == (1, 2)
    assert await scalar(sessionmaker, "SELECT count(*) FROM page_embeddings") == 1


async def test_identity_unavailable_counts_one_failure(sessionmaker: SM) -> None:
    await seed(sessionmaker, A)
    provider = FakeEmbedder()
    provider.capability_model = None  # the provider reports no embedding model
    detail = await analyse(sessionmaker, provider)
    assert detail.status is RunStatus.COMPLETED_WITH_ERRORS
    assert detail.counters["embedding_failures"] == 1
    assert provider.embed_calls == []


async def test_model_identity_mismatch_is_never_stored(sessionmaker: SM) -> None:
    await seed(sessionmaker, A, B, C)
    provider = FakeEmbedder()
    provider.result_model = "some-other-model"  # capabilities say fake-embed-v1
    detail = await analyse(sessionmaker, provider)
    assert detail.status is RunStatus.COMPLETED_WITH_ERRORS
    assert (detail.counters["embeddings_written"], detail.counters["embedding_failures"]) == (0, 3)
    assert await scalar(sessionmaker, "SELECT count(*) FROM page_embeddings") == 0


async def test_new_page_enters_an_old_pages_top_k(sessionmaker: SM) -> None:
    settings = make_settings(similarity_top_k=1, similarity_min_score=0.3, embedding_batch_size=4)
    old_a = page("/reports/a", "alpha alpha alpha beta")
    old_b = page("/reports/b", "alpha beta beta gamma")
    await seed(sessionmaker, old_a, old_b)
    await analyse(sessionmaker, settings=settings)
    assert await edges(sessionmaker, SEMANTIC) == {
        ("/reports/a", "/reports/b", SEMANTIC),
        ("/reports/b", "/reports/a", SEMANTIC),
    }
    # D is almost identical to A: it must REPLACE B as A's single neighbour,
    # although A itself was not analysed in this run.
    await seed(sessionmaker, page("/reports/d", "alpha alpha alpha beta beta"))
    detail = await analyse(sessionmaker, settings=settings)
    assert detail.counters["pages_analyzed"] == 1
    semantic = await edges(sessionmaker, SEMANTIC)
    assert ("/reports/a", "/reports/d", SEMANTIC) in semantic
    assert ("/reports/a", "/reports/b", SEMANTIC) not in semantic
    assert {(s, k) for s, _, k in semantic} == {
        ("/reports/a", SEMANTIC),
        ("/reports/b", SEMANTIC),
        ("/reports/d", SEMANTIC),
    }


async def test_force_recomputes_without_rewriting_history(sessionmaker: SM) -> None:
    await seed(sessionmaker, A, B, C)
    await analyse(sessionmaker)
    analyses = await rows(sessionmaker, "SELECT id, created_at FROM page_analyses ORDER BY id")
    embeddings = await rows(sessionmaker, "SELECT id FROM page_embeddings ORDER BY id")
    stored = await edges(sessionmaker)
    detail = await analyse(sessionmaker, force=True)
    assert detail.status is RunStatus.SUCCEEDED
    c = detail.counters
    assert (c["pages_analyzed"], c["pages_fresh_skipped"], c["embeddings_written"]) == (0, 3, 0)
    assert c["relationships_written"] == len(stored)
    assert await rows(sessionmaker, "SELECT id, created_at FROM page_analyses ORDER BY id") == (
        analyses
    )
    assert await rows(sessionmaker, "SELECT id FROM page_embeddings ORDER BY id") == embeddings
    assert await edges(sessionmaker) == stored
    assert (
        await scalar(sessionmaker, "SELECT count(DISTINCT run_id) FROM page_relationships") == 1
    )  # every edge was rewritten by the forced run
    assert await scalar(sessionmaker, "SELECT min(run_id::text) FROM page_relationships") == str(
        detail.id
    )


async def test_a_failing_page_is_counted_and_the_run_continues(sessionmaker: SM) -> None:
    await seed(sessionmaker, A, B, C)
    analyzer = FakeAnalyzer(fail_for={"https://site.test/reports/b"})
    detail = await analyse(sessionmaker, analyzer=analyzer)
    assert detail.status is RunStatus.COMPLETED_WITH_ERRORS
    assert (detail.counters["pages_analyzed"], detail.counters["errors"]) == (2, 1)
    assert detail.failure_reason is None
    assert "postgresql" not in json.dumps(detail.model_dump(mode="json"))
    events = await _job_events(sessionmaker)
    payload = JobCompletedPayload.model_validate(events[0].payload)
    assert payload.status is RunStatus.COMPLETED_WITH_ERRORS
    assert payload.counters["errors"] == 1
    # The next (healthy) run retries the page whose analysis is missing.
    retry = await analyse(sessionmaker)
    assert (retry.counters["pages_analyzed"], retry.counters["pages_fresh_skipped"]) == (1, 2)


async def test_no_embeddings_never_calls_the_model(sessionmaker: SM) -> None:
    await seed(sessionmaker, A, B)
    provider = FakeEmbedder()
    detail = await analyse(sessionmaker, provider, embeddings=False)
    assert detail.status is RunStatus.SUCCEEDED
    assert provider.embed_calls == []
    assert detail.counters["embeddings_written"] == detail.counters["embedding_failures"] == 0
    assert await edges(sessionmaker, SEMANTIC) == set()


async def test_page_ids_restrict_the_scope(sessionmaker: SM) -> None:
    await seed(sessionmaker, A, B, C)
    ids = await page_ids(sessionmaker)
    detail = await analyse(sessionmaker, page_ids=[ids["/reports/a"]])
    assert (detail.counters["pages_considered"], detail.counters["pages_analyzed"]) == (1, 1)
    assert detail.config_snapshot["page_ids"] == [str(ids["/reports/a"])]


async def test_snapshot_and_event_are_secret_free_and_governed(sessionmaker: SM) -> None:
    await seed(sessionmaker, A)
    detail = await analyse(sessionmaker)
    snapshot = json.dumps(detail.config_snapshot)
    assert "postgresql" not in snapshot and "ken:ken" not in snapshot
    assert detail.config_snapshot["analysis_version"] == AV
    assert detail.config_snapshot["embedding"]["recipe_fingerprint"] == FINGERPRINT
    assert detail.config_snapshot["relationships"] == {
        "max_per_type": 25,
        "materialised_types": [
            "complementary_market",
            "parent_market",
            "related_market",
            "same_market",
            "submarket",
            "supply_chain_downstream",
            "supply_chain_upstream",
        ],
    }
    events = await _job_events(sessionmaker)
    assert len(events) == 1
    event = events[0]
    assert (event.subject_type, event.subject_id, event.emitted_by) == (
        "job_run",
        str(detail.id),
        "ken-jobs",
    )
    assert event.correlation_id == detail.id
    assert JobCompletedPayload.model_validate(event.payload).counters == detail.counters


async def _job_events(sessionmaker: SM) -> list:  # type: ignore[type-arg]
    async with sessionmaker() as session:
        return await list_events(session, event_name=EventName.JOB_COMPLETED)


async def test_smoke_with_the_real_toolkit_policy_and_mock_provider(sessionmaker: SM) -> None:
    """Wiring check with the deterministic modules (assertions are structural only)."""
    from ken_core.providers.model.mock import MockModelProvider
    from ken_services.intelligence.pipeline import run_analysis
    from tests.integration.conftest import TickClock

    body = (
        "The India electric vehicle market report covers battery demand, charging and "
        "two-wheeler adoption across Asia. " * 4
    )
    await seed(
        sessionmaker,
        page("/reports/india-ev", body, title="India Electric Vehicle Market"),
        page("/reports/india-batteries", body + " lithium-ion cells", title="India Batteries"),
        page("/privacy", "Privacy policy text " * 20, title="Privacy"),
    )
    settings = make_settings(mock_embedding_dim=32)
    provider = MockModelProvider(settings)
    detail = await run_analysis(
        sessionmaker=sessionmaker,
        settings=settings,
        model_provider=provider,
        triggered_by="test",
        clock=TickClock(),
    )
    assert detail.status is RunStatus.SUCCEEDED, detail.counters
    assert (detail.counters["pages_analyzed"], detail.counters["embeddings_written"]) == (3, 3)
    assert await scalar(sessionmaker, "SELECT count(DISTINCT dimensions) FROM page_embeddings") == 1
    assert await scalar(sessionmaker, "SELECT min(dimensions) FROM page_embeddings") == 32
    # The policy's legal-pages rule protects /privacy.
    assert detail.counters["protections_added"] >= 1
    again = await run_analysis(
        sessionmaker=sessionmaker,
        settings=settings,
        model_provider=provider,
        triggered_by="test",
        clock=TickClock(),
    )
    assert (again.counters["pages_analyzed"], again.counters["embeddings_written"]) == (0, 0)
    assert again.counters["protections_added"] == again.counters["protections_deactivated"] == 0


# --------------------------------------------------------------------------- #
# Embedding recipe fingerprint (QA M1)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "change", [{"embedding_document_prefix": "passage: "}, {"embedding_max_chars": 5000}]
)
async def test_a_recipe_setting_change_makes_embeddings_stale(
    sessionmaker: SM, change: dict[str, object]
) -> None:
    base = {"similarity_top_k": 3, "similarity_min_score": 0.5, "embedding_batch_size": 2}
    await seed(sessionmaker, A, B, C)
    await analyse(sessionmaker, settings=make_settings(**base))
    changed = make_settings(**base, **change)
    new_fingerprint = current_recipe_fingerprint(changed)
    assert new_fingerprint != FINGERPRINT

    provider = FakeEmbedder()
    detail = await analyse(sessionmaker, provider, settings=changed)
    assert detail.status is RunStatus.SUCCEEDED, detail.counters
    c = detail.counters
    assert (c["pages_analyzed"], c["embeddings_written"], c["embeddings_fresh_skipped"]) == (
        0,
        3,
        0,
    )
    assert detail.config_snapshot["embedding"]["recipe_fingerprint"] == new_fingerprint
    if "embedding_document_prefix" in change:
        assert all(t.startswith("passage: ") for call in provider.embed_calls for t in call)
    # A NEW row per page; the old rows are immutable and stay.
    stored = await rows(
        sessionmaker,
        "SELECT recipe_fingerprint, count(*) FROM page_embeddings GROUP BY 1 ORDER BY 1",
    )
    assert {str(f): int(n) for f, n in stored} == {FINGERPRINT: 3, new_fingerprint: 3}
    # Semantic edges were recomputed over the new recipe's vectors only.
    assert await edges(sessionmaker, SEMANTIC) == {
        ("/reports/a", "/reports/b", SEMANTIC),
        ("/reports/b", "/reports/a", SEMANTIC),
    }
    assert {
        r[0]
        for r in await rows(
            sessionmaker,
            "SELECT DISTINCT run_id FROM page_relationships "
            "WHERE relationship_type = 'semantic_similar'",
        )
    } == {detail.id}

    again = await analyse(sessionmaker, settings=changed)
    assert (again.counters["embeddings_written"], again.counters["embeddings_fresh_skipped"]) == (
        0,
        3,
    )
    # Going back to the original recipe finds its rows fresh again: nothing re-embedded.
    back = await analyse(sessionmaker, settings=make_settings(**base))
    assert (back.counters["embeddings_written"], back.counters["embeddings_fresh_skipped"]) == (
        0,
        3,
    )


async def test_semantic_edges_never_join_two_recipes(sessionmaker: SM) -> None:
    base = {"similarity_top_k": 3, "similarity_min_score": 0.5, "embedding_batch_size": 2}
    await seed(sessionmaker, A, B, page("/reports/d", "alpha alpha alpha beta beta"))
    await analyse(sessionmaker, settings=make_settings(**base))
    assert len(await edges(sessionmaker, SEMANTIC)) == 6  # A, B, D are all alike
    # The recipe changes, and the first batch (two pages, by id) fails to re-embed:
    # only ONE page has a vector of the new recipe.
    provider = FakeEmbedder()
    provider.fail_on_calls = {1}
    changed = make_settings(**base, embedding_document_prefix="passage: ")
    detail = await analyse(sessionmaker, provider, settings=changed)
    assert (detail.counters["embeddings_written"], detail.counters["embedding_failures"]) == (1, 2)
    [moved] = await rows(
        sessionmaker,
        "SELECT p.canonical_url FROM page_embeddings e JOIN pages p ON p.id = e.page_id "
        "WHERE e.recipe_fingerprint = :f",
        f=current_recipe_fingerprint(changed),
    )
    path = str(moved[0]).removeprefix("https://site.test")
    # It is never compared with the old recipe's vectors: no semantic edge touches it.
    semantic = await edges(sessionmaker, SEMANTIC)
    assert all(path not in (source, target) for source, target, _ in semantic)
    # Its old neighbours were recomputed under the CURRENT recipe, where they have
    # no vector yet: they get no edges until re-embedded (never an old-recipe edge).
    assert semantic == set()
    healed = await analyse(sessionmaker, settings=changed)
    assert healed.counters["embeddings_written"] == 2
    assert len(await edges(sessionmaker, SEMANTIC)) == 6


async def test_switching_back_to_an_old_recipe_recomputes_semantic_edges(sessionmaker: SM) -> None:
    """QA re-review: re-using stored vectors (0 embeddings written) must still rebuild the edges.

    Every semantic edge records the (model, recipe fingerprint) that produced it; edges from
    another identity are recomputed, and readers never serve them.
    """
    base = {"similarity_top_k": 3, "similarity_min_score": 0.5, "embedding_batch_size": 2}
    await seed(sessionmaker, A, B, C)
    await analyse(sessionmaker, settings=make_settings(**base))
    other = make_settings(**base, embedding_max_chars=3000)
    await analyse(sessionmaker, settings=other)
    back = await analyse(sessionmaker, settings=make_settings(**base))
    assert back.counters["embeddings_written"] == 0  # the original vectors are reused
    fingerprints = {
        str(r[0])
        for r in await rows(
            sessionmaker,
            "SELECT DISTINCT evidence->>'recipe_fingerprint' FROM page_relationships "
            "WHERE relationship_type = 'semantic_similar'",
        )
    }
    assert fingerprints == {FINGERPRINT}
    assert await edges(sessionmaker, SEMANTIC) == {
        ("/reports/a", "/reports/b", SEMANTIC),
        ("/reports/b", "/reports/a", SEMANTIC),
    }


# --------------------------------------------------------------------------- #
# Bounded taxonomy graph (QA B2)
# --------------------------------------------------------------------------- #
MATERIALISED_COUNT = 7  # relationships.MATERIALISED_RELATIONSHIP_TYPES


def _many(n: int, *, market: str = "ev", prefix: str = "p") -> list[object]:
    """``n`` pages in ONE market (the worst case); every third page also names India."""
    return [
        page(
            f"/reports/{prefix}{i:03d}",
            f"alpha market:{market}" + (" geo:india" if i % 3 == 0 else ""),
        )
        for i in range(n)
    ]


async def _per_source_type(sessionmaker: SM) -> dict[tuple[str, str], int]:
    found = await rows(
        sessionmaker,
        "SELECT source_page_id::text, relationship_type, count(*) FROM page_relationships "
        "GROUP BY 1, 2",
    )
    return {(s, k): int(n) for s, k, n in found}


async def test_taxonomy_edges_are_capped_per_source_and_type_at_scale(sessionmaker: SM) -> None:
    pages = 300
    cap = 5
    settings = make_settings(relationship_max_per_type=cap)
    await seed(sessionmaker, *_many(pages))
    detail = await analyse(sessionmaker, settings=settings, embeddings=False)
    assert detail.status is RunStatus.SUCCEEDED, detail.counters
    assert detail.counters["pages_analyzed"] == pages

    total = await scalar(sessionmaker, "SELECT count(*) FROM page_relationships")
    assert total == detail.counters["relationships_written"]
    # Uncapped this would be 300 x 299 same_market + 100 x 99 same_geography edges.
    assert total <= pages * MATERIALISED_COUNT * cap
    assert total == pages * cap  # every page has >= cap same_market candidates
    kinds = {
        r[0]
        for r in await rows(
            sessionmaker, "SELECT DISTINCT relationship_type FROM page_relationships"
        )
    }
    assert kinds == {"same_market"} and not kinds & COARSE
    assert max((await _per_source_type(sessionmaker)).values()) <= cap

    # The kept edges are the top ones: weight (all 1.0 here), then shared
    # geographies, then target page id.
    ids = await page_ids(sessionmaker)
    india = {ids[f"/reports/p{i:03d}"] for i in range(pages) if i % 3 == 0}
    everyone = sorted(ids.values())
    stored = await rows(
        sessionmaker, "SELECT source_page_id, target_page_id FROM page_relationships"
    )
    targets: dict[object, set[object]] = {}
    for source, target in stored:
        targets.setdefault(source, set()).add(target)
    for source in everyone:
        if source in india:
            expected = [p for p in everyone if p in india and p != source][:cap]
        else:
            expected = [p for p in everyone if p != source][:cap]
        assert targets[source] == set(expected)


async def test_an_incremental_run_keeps_the_capped_graph_exact(sessionmaker: SM) -> None:
    """After a page changes, the incremental result equals a full recomputation."""
    cap = 3
    settings = make_settings(relationship_max_per_type=cap)
    corpus = _many(24) + _many(6, market="batteries", prefix="b")
    await seed(sessionmaker, *corpus)
    await analyse(sessionmaker, settings=settings, embeddings=False)
    ids = await page_ids(sessionmaker)
    assert len(ids) == 30
    # The page with the LOWEST id was in many top-3 lists; it leaves the market, so
    # pages that never touched it in this run must be recomputed to refill their caps.
    lowest = min(ids.items(), key=lambda kv: kv[1])[0]
    await seed(sessionmaker, page(lowest, "alpha market:batteries geo:india"))
    incremental = await analyse(sessionmaker, settings=settings, embeddings=False)
    assert incremental.counters["pages_analyzed"] == 1
    after_incremental = await edges(sessionmaker)
    assert max((await _per_source_type(sessionmaker)).values()) <= cap

    forced = await analyse(sessionmaker, settings=settings, embeddings=False, force=True)
    assert forced.status is RunStatus.SUCCEEDED
    assert await edges(sessionmaker) == after_incremental
    stale = await scalar(
        sessionmaker,
        "SELECT count(*) FROM page_relationships r JOIN pages s ON s.id = r.source_page_id "
        "JOIN pages t ON t.id = r.target_page_id "
        "WHERE r.source_page_version <> s.current_version "
        "OR r.target_page_version <> t.current_version",
    )
    assert stale == 0
