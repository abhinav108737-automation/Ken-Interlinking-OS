"""Phase 2 offline retrieval eval over ``tests/fixtures/demo/relatedness.json``.

Two channels plus their combination rank every pool page for every query page:

* **taxonomy** -- sum of ``taxonomy_relationships`` weights (ADR-0010 s4);
* **embedding** -- cosine of ``MockModelProvider`` embeddings of the
  ``embedding_text`` recipe. ADR-0010 s2 makes mock embeddings feature hashing
  over word uni- and bigrams, so they are lexically meaningful and deterministic;
* **combined** -- per-query min-max-normalised taxonomy + embedding.

Metrics: recall@5 / recall@10 of ``strong`` partners, MRR of the first strong
partner, mean ranks, and the share of (query, strong, unrelated) triples ranked
the right way round. See ``evals/intelligence/README.md`` for the table.

FLOORS ARE A REGRESSION GATE, NOT A TARGET. Each floor sits 0.05-0.10 below the
value measured when the eval was written (2026-10-14, mock provider, 768
dimensions, taxonomy v1, recipe v1), rounded down to 0.05. A drop below a floor
means a change in the analyser, the relationship rules, the embedding recipe or
the mock's hashing made retrieval measurably worse on labelled data: investigate
before touching the number. Raising a floor after a genuine improvement is fine;
lowering one is a reviewed decision (AUTONOMY_AND_PERMISSIONS.md: weakening a
test is BLOCK-level).
"""

from __future__ import annotations

import asyncio
from functools import cache

import pytest

from ken_core.config import Settings
from ken_core.providers.model.mock import MockModelProvider
from tests.unit.intelligence_evals import demo_corpus as demo

# measured (2026-10-14):  r@5    r@10   MRR    pairwise
#   taxonomy              0.915  0.988  0.893  0.985
#   embedding (mock)      0.788  0.932  0.944  0.892
#   combined              0.959  0.996  0.946  0.992
FLOORS: dict[str, dict[str, float]] = {
    "taxonomy": {"recall_at_5": 0.85, "recall_at_10": 0.90, "mrr": 0.80, "pairwise": 0.90},
    "embedding": {"recall_at_5": 0.70, "recall_at_10": 0.85, "mrr": 0.85, "pairwise": 0.80},
    "combined": {"recall_at_5": 0.85, "recall_at_10": 0.90, "mrr": 0.85, "pairwise": 0.90},
}
MIN_QUERIES = 40


def _settings() -> Settings:
    # No .env: the eval must not depend on a developer's local configuration.
    return Settings(_env_file=None, model_provider="mock")


@cache
def _embedding_run() -> demo.EmbeddingRun:
    settings = _settings()

    async def run() -> demo.EmbeddingRun:
        provider = MockModelProvider(settings)
        try:
            return await demo.embed_pages(
                provider,
                demo.load_relatedness().pool,
                max_chars=settings.embedding_max_chars,
                prefix=settings.embedding_document_prefix,
                batch_size=settings.embedding_batch_size,
            )
        finally:
            await provider.aclose()

    return asyncio.run(run())


@cache
def _metrics() -> dict[str, demo.ChannelMetrics]:
    return demo.evaluate_all(_embedding_run().vectors)


def test_relatedness_labels_are_well_formed() -> None:
    doc = demo.load_json(demo.RELATEDNESS_PATH)
    pool = set(doc["pool"])
    seen: set[frozenset[str]] = set()
    counts = {"strong": 0, "weak": 0, "unrelated": 0}
    for pair in doc["pairs"]:
        key = frozenset((pair["a"], pair["b"]))
        assert len(key) == 2 and key not in seen, pair
        assert key <= pool, pair
        assert pair["label"] in counts and pair["rationale"].strip(), pair
        seen.add(key)
        counts[pair["label"]] += 1
    assert sum(counts.values()) >= 80
    assert min(counts.values()) >= 15, counts
    hard = [p for p in doc["pairs"] if p["rationale"].startswith("Hard negative")]
    assert len(hard) >= 15


def test_mock_embeddings_are_deterministic_and_provider_dimensioned() -> None:
    run = _embedding_run()
    assert run.dimensions == _settings().mock_embedding_dim
    assert run.model == run.capabilities_model, "embedding identity must be provider-reported"
    assert all(len(v) == run.dimensions for v in run.vectors.values())
    _embedding_run.cache_clear()
    assert _embedding_run().vectors == run.vectors


@pytest.mark.parametrize("channel", demo.CHANNELS)
def test_channel_meets_floors(channel: str) -> None:
    metrics = _metrics()[channel]
    floor = FLOORS[channel]
    assert metrics.queries >= MIN_QUERIES
    measured = {
        "recall_at_5": metrics.recall_at_5,
        "recall_at_10": metrics.recall_at_10,
        "mrr": metrics.mrr,
        "pairwise": metrics.pairwise_strong_over_unrelated,
    }
    below = {k: (measured[k], floor[k]) for k in floor if measured[k] < floor[k]}
    assert not below, f"{channel} below floor (measured, floor): {below}"


@pytest.mark.parametrize("channel", demo.CHANNELS)
def test_unrelated_pairs_rank_below_strong_on_average(channel: str) -> None:
    metrics = _metrics()[channel]
    assert metrics.mean_rank_strong < metrics.mean_rank_weak < metrics.mean_rank_unrelated
    assert metrics.mean_score_strong > metrics.mean_score_unrelated


def test_combined_is_not_worse_than_best_single_channel_at_10() -> None:
    metrics = _metrics()
    best_single = max(metrics["taxonomy"].recall_at_10, metrics["embedding"].recall_at_10)
    assert metrics["combined"].recall_at_10 >= best_single, {
        name: m.recall_at_10 for name, m in metrics.items()
    }


def test_report_metrics(capsys: pytest.CaptureFixture[str]) -> None:
    """Prints the table (visible with -rA / -s); always passes once metrics exist."""
    with capsys.disabled():
        print()
        for name, m in _metrics().items():
            print(
                f"  {name:9s} queries={m.queries} r@5={m.recall_at_5:.3f} "
                f"r@10={m.recall_at_10:.3f} mrr={m.mrr:.3f} "
                f"rank(strong/weak/unrel)={m.mean_rank_strong:.1f}/{m.mean_rank_weak:.1f}/"
                f"{m.mean_rank_unrelated:.1f} pairwise={m.pairwise_strong_over_unrelated:.3f}"
            )
