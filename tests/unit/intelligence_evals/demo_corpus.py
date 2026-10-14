"""Shared, deterministic helpers for the Phase 2 demo-corpus evals.

Used by ``test_analysis_goldens.py``, ``test_retrieval_eval.py`` and
``evals/intelligence/benchmark.py``. Pure functions over the committed fixtures:
no database, no network (the benchmark alone may reach the LOCAL Ollama daemon,
through the provider it is handed).

Pipeline, mirroring what Phase 2 does to a stored page version::

    research_site.jsonl --FileExportContentSource--> SourceDocument
        --normalize_document--> NormalizedPage
        --AnalysisInput--> analyze_page --> PageAnalysis
        --embedding_text--> ModelProvider.embed --> cosine ranking

Retrieval channels (each ranks every pool page except the query):

* ``taxonomy``  sum of ``taxonomy_relationships(query, candidate)`` weights;
* ``embedding`` cosine similarity of the page embeddings;
* ``combined``  per-query min-max-normalised taxonomy + embedding (simple sum).

Ties are broken by canonical URL (ascending) so every ranking is reproducible.
That matters for the taxonomy channel, whose scores are coarse.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any, Final

from ken_contracts.intelligence import PageAnalysis, TaxonomyPolicy
from ken_contracts.inventory import NormalizedPage, SourceDocument
from ken_contracts.model import ModelProvider, ModelRole
from ken_services.intelligence.analyze import AnalysisInput, analyze_page
from ken_services.intelligence.eligibility import EligibilityState
from ken_services.intelligence.embedding_text import embedding_text
from ken_services.intelligence.protection import match_protections
from ken_services.intelligence.relationships import taxonomy_relationships
from ken_services.intelligence.taxonomy import load_taxonomy
from ken_services.inventory.normalize import normalize_document
from ken_services.inventory.sources.file_export import FileExportContentSource

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
DEMO_DIR: Final = REPO_ROOT / "tests" / "fixtures" / "demo"
CORPUS_PATH: Final = DEMO_DIR / "research_site.jsonl"
RELATEDNESS_PATH: Final = DEMO_DIR / "relatedness.json"
GOLDENS_PATH: Final = DEMO_DIR / "analysis_expected.json"
TAXONOMY_PATH: Final = REPO_ROOT / "policies" / "taxonomy" / "taxonomy.v1.yaml"

FIXED_TIME: Final = datetime(2026, 10, 14, 0, 0, tzinfo=UTC)
PAGE_ID_NAMESPACE: Final = uuid.UUID("6f2c1d4e-0d1a-5b8e-9c7a-2d3e4f5a6b7c")
CHANNELS: Final = ("taxonomy", "embedding", "combined")
KS: Final = (5, 10)


# --------------------------------------------------------------------------- #
# Corpus
# --------------------------------------------------------------------------- #
def page_id_for(url: str) -> uuid.UUID:
    """A stable synthetic page id (the DB mints random ones; evals need fixed ones)."""
    return uuid.uuid5(PAGE_ID_NAMESPACE, url)


async def _read_corpus(path: Path) -> list[NormalizedPage]:
    source = FileExportContentSource(path, max_bytes=20_000_000, clock=lambda: FIXED_TIME)
    try:
        scope = await source.site_scope()
        pages: list[NormalizedPage] = []
        async for record in source.iter_records():
            if not isinstance(record, SourceDocument):
                raise AssertionError(f"demo corpus produced an issue: {record.code}")
            pages.append(normalize_document(record, scope=scope))
        return pages
    finally:
        await source.aclose()


@cache
def load_pages() -> tuple[NormalizedPage, ...]:
    """Every corpus record, normalised by the Phase 1 normaliser, in file order."""
    return tuple(asyncio.run(_read_corpus(CORPUS_PATH)))


@cache
def taxonomy() -> TaxonomyPolicy:
    return load_taxonomy(TAXONOMY_PATH)


def pages_by_url() -> dict[str, NormalizedPage]:
    """Keyed by the page's own (normalised) URL, which is unique in the export."""
    return {page.url: page for page in load_pages()}


def analysis_input(page: NormalizedPage) -> AnalysisInput:
    return AnalysisInput(
        page_id=page_id_for(page.url),
        page_version=1,
        url=page.url,
        title=page.title,
        meta_description=page.meta_description,
        headings=tuple((h.level, h.text) for h in page.headings),
        body_html=page.body_html,
        body_text=page.body_text,
        metadata=dict(page.metadata),
    )


@cache
def analyses() -> dict[str, PageAnalysis]:
    policy = taxonomy()
    return {page.url: analyze_page(analysis_input(page), taxonomy=policy) for page in load_pages()}


def protection_rule_ids(page: NormalizedPage) -> list[str]:
    matches = match_protections(url=page.url, metadata=page.metadata, taxonomy=taxonomy())
    return [m.rule_id for m in matches]


def eligibility_state(page: NormalizedPage, analysis: PageAnalysis) -> EligibilityState:
    """Current-state facts as Phase 2 would read them, with a FRESH analysis."""
    return EligibilityState(
        publication_state=page.publication_state,
        gone=False,
        # Policy rules OR the CMS's own flag, which ingestion mirrors as a source=cms row.
        protected=bool(protection_rule_ids(page)) or page.metadata.get("cms_protected") == "true",
        robots_directives=page.robots_directives,
        canonical_status=page.canonical_status,
        http_status=page.http_status,
        word_count=page.word_count,
        page_type=analysis.page_type,
        has_analysis=True,
        analysis_fresh=True,
    )


def load_json(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text("utf-8"))
    return data


# --------------------------------------------------------------------------- #
# Labelled pairs
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Relatedness:
    pool: tuple[str, ...]
    labels: dict[frozenset[str], str]

    def partners(self, url: str, label: str) -> set[str]:
        return {
            other
            for pair, lab in self.labels.items()
            if lab == label and url in pair
            for other in pair
            if other != url
        }


def load_relatedness() -> Relatedness:
    doc = load_json(RELATEDNESS_PATH)
    labels = {frozenset((p["a"], p["b"])): p["label"] for p in doc["pairs"]}
    return Relatedness(pool=tuple(doc["pool"]), labels=labels)


# --------------------------------------------------------------------------- #
# Embeddings
# --------------------------------------------------------------------------- #
def document_text(page: NormalizedPage, *, max_chars: int, prefix: str) -> str:
    return embedding_text(
        title=page.title,
        meta_description=page.meta_description,
        headings=[h.text for h in page.headings],
        body_text=page.body_text,
        max_chars=max_chars,
        prefix=prefix,
    )


@dataclass(frozen=True, slots=True)
class EmbeddingRun:
    vectors: dict[str, list[float]]
    model: str
    capabilities_model: str | None
    provider: str
    dimensions: int
    batch_latency_ms: list[float]


async def embed_pages(
    provider: ModelProvider,
    urls: Sequence[str],
    *,
    max_chars: int,
    prefix: str,
    batch_size: int,
    clock: Callable[[], float] = time.perf_counter,
) -> EmbeddingRun:
    """Embed every page in ``urls`` in batches; the dimension comes from the result."""
    by_url = pages_by_url()
    texts = [document_text(by_url[u], max_chars=max_chars, prefix=prefix) for u in urls]
    capabilities = await provider.capabilities()
    caps_model = next((r.model for r in capabilities.roles if r.role is ModelRole.EMBEDDING), None)
    vectors: dict[str, list[float]] = {}
    latencies: list[float] = []
    model = provider_name = ""
    dimensions = 0
    for start in range(0, len(texts), batch_size):
        began = clock()
        result = await provider.embed(texts=texts[start : start + batch_size])
        latencies.append(round((clock() - began) * 1000.0, 3))
        if dimensions and result.dimensions != dimensions:
            raise AssertionError("embedding dimension changed between batches")
        model, provider_name, dimensions = result.model, result.provider, result.dimensions
        for url, vector in zip(urls[start : start + batch_size], result.vectors, strict=True):
            vectors[url] = vector
    return EmbeddingRun(
        vectors=vectors,
        model=model,
        capabilities_model=caps_model,
        provider=provider_name,
        dimensions=dimensions,
        batch_latency_ms=latencies,
    )


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = math.fsum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(math.fsum(x * x for x in a))
    nb = math.sqrt(math.fsum(y * y for y in b))
    return 0.0 if na == 0.0 or nb == 0.0 else dot / (na * nb)


# --------------------------------------------------------------------------- #
# Ranking and metrics
# --------------------------------------------------------------------------- #
type Scores = dict[str, dict[str, float]]
"""``scores[query][candidate]`` for one channel."""


def taxonomy_scores(pool: Sequence[str], analysed: Mapping[str, PageAnalysis]) -> Scores:
    policy = taxonomy()
    return {
        q: {
            c: math.fsum(
                r.weight for r in taxonomy_relationships(analysed[q], analysed[c], taxonomy=policy)
            )
            for c in pool
            if c != q
        }
        for q in pool
    }


def embedding_scores(pool: Sequence[str], vectors: Mapping[str, Sequence[float]]) -> Scores:
    return {q: {c: cosine(vectors[q], vectors[c]) for c in pool if c != q} for q in pool}


def _minmax(row: Mapping[str, float]) -> dict[str, float]:
    lo, hi = min(row.values()), max(row.values())
    span = hi - lo
    return {k: (v - lo) / span if span > 0 else 0.0 for k, v in row.items()}


def combined_scores(*channels: Scores) -> Scores:
    first = channels[0]
    out: Scores = {}
    for q in first:
        normed = [_minmax(ch[q]) for ch in channels]
        out[q] = {c: math.fsum(n[c] for n in normed) for c in first[q]}
    return out


def ranking(row: Mapping[str, float]) -> list[str]:
    return sorted(row, key=lambda c: (-row[c], c))


@dataclass(frozen=True, slots=True)
class ChannelMetrics:
    queries: int
    recall_at_5: float
    recall_at_10: float
    mrr: float
    mean_rank_strong: float
    mean_rank_weak: float
    mean_rank_unrelated: float
    mean_score_strong: float
    mean_score_unrelated: float
    pairwise_strong_over_unrelated: float
    """Share of (query, strong partner, unrelated partner) triples ranked correctly."""

    def as_dict(self) -> dict[str, float | int]:
        return {k: getattr(self, k) for k in self.__slots__}


def evaluate(scores: Scores, rel: Relatedness) -> ChannelMetrics:
    recall: dict[int, list[float]] = {k: [] for k in KS}
    reciprocal: list[float] = []
    ranks: dict[str, list[int]] = {"strong": [], "weak": [], "unrelated": []}
    values: dict[str, list[float]] = {"strong": [], "unrelated": []}
    triples_ok = triples = 0
    for q in rel.pool:
        order = ranking(scores[q])
        position = {c: i + 1 for i, c in enumerate(order)}
        strong = rel.partners(q, "strong") & position.keys()
        for label, bucket in ranks.items():
            bucket.extend(position[c] for c in rel.partners(q, label) & position.keys())
        for label, vbucket in values.items():
            vbucket.extend(scores[q][c] for c in rel.partners(q, label) & position.keys())
        unrelated = rel.partners(q, "unrelated") & position.keys()
        for s in strong:
            for u in unrelated:
                triples += 1
                triples_ok += position[s] < position[u]
        if not strong:
            continue
        for k in KS:
            recall[k].append(len(strong & set(order[:k])) / len(strong))
        reciprocal.append(1.0 / min(position[s] for s in strong))

    def mean(xs: Sequence[float]) -> float:
        return round(math.fsum(xs) / len(xs), 4) if xs else float("nan")

    return ChannelMetrics(
        queries=len(reciprocal),
        recall_at_5=mean(recall[5]),
        recall_at_10=mean(recall[10]),
        mrr=mean(reciprocal),
        mean_rank_strong=mean(ranks["strong"]),
        mean_rank_weak=mean(ranks["weak"]),
        mean_rank_unrelated=mean(ranks["unrelated"]),
        mean_score_strong=mean(values["strong"]),
        mean_score_unrelated=mean(values["unrelated"]),
        pairwise_strong_over_unrelated=round(triples_ok / triples, 4) if triples else float("nan"),
    )


def evaluate_all(vectors: Mapping[str, Sequence[float]]) -> dict[str, ChannelMetrics]:
    """Metrics for every channel over the relatedness pool."""
    rel = load_relatedness()
    analysed = analyses()
    tax = taxonomy_scores(rel.pool, analysed)
    emb = embedding_scores(rel.pool, vectors)
    channels = {"taxonomy": tax, "embedding": emb, "combined": combined_scores(tax, emb)}
    return {name: evaluate(channels[name], rel) for name in CHANNELS}
