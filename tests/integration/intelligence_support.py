"""Fakes for Phase 2 integration tests: a toy taxonomy, a scripted analyser and a
controllable embedding provider.

Pipeline and reader tests must not depend on the deterministic analysis core
(a separate workstream): the fake analyser reads tokens from the body text --
``market:<id>``, ``geo:<id>``, ``type:<id>`` -- and the fake provider embeds
texts as bag-of-words counts over a tiny vocabulary, so cosine similarity is
fully under the test's control.
"""

from __future__ import annotations

import hashlib
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.intelligence import (
    GeographyMention,
    Intent,
    PageAnalysis,
    ProtectionSource,
    RelationshipType,
    TaxonomyPolicy,
    TopicScore,
    analysis_version,
)
from ken_contracts.inventory import IngestionRunDetail
from ken_contracts.jobs import JobRunDetail
from ken_contracts.model import (
    Completion,
    EmbeddingResult,
    GenerationOptions,
    Message,
    ModelRole,
    ModelRoleStatus,
    ProviderCapabilities,
    ProviderHealth,
    StructuredResult,
)
from ken_services.intelligence.embeddings import EmbeddingRecipe
from ken_services.intelligence.pipeline import AnalysisToolkit, run_analysis
from tests.conftest import make_settings
from tests.integration.conftest import TickClock, ingest, make_page

TAXONOMY = TaxonomyPolicy.model_validate(
    {
        "policy": "taxonomy",
        "version": 7,
        "status": "test",
        "source": "fixture",
        "markets": [
            {"id": "automotive", "label": "Automotive"},
            {"id": "ev", "label": "Electric vehicles", "parent": "automotive"},
            {"id": "batteries", "label": "Batteries"},
        ],
        "geographies": [
            {"id": "global", "label": "Global", "kind": "global"},
            {"id": "asia", "label": "Asia", "kind": "region", "parent": "global"},
            {"id": "india", "label": "India", "kind": "country", "parent": "asia"},
        ],
        "page_types": [
            {
                "id": "report",
                "label": "Report",
                "path_patterns": ["^/reports/"],
                "intent": "commercial_research",
                "commercial_importance": 3,
            },
            {
                "id": "other",
                "label": "Other",
                "path_patterns": ["^/$"],
                "intent": "unknown",
                "commercial_importance": 0,
            },
        ],
        "default_page_type": "other",
        "protections": [
            {"id": "locked", "reason": "Locked by metadata.", "metadata_equals": {"lock": "yes"}},
        ],
    }
)
AV = analysis_version(TAXONOMY.version)
VOCABULARY = ("alpha", "beta", "gamma", "delta")
EMBED_MODEL = "fake-embed-v1"


# --------------------------------------------------------------------------- #
# Analyser and toolkit
# --------------------------------------------------------------------------- #
_ROOTS = {"automotive": "automotive", "ev": "automotive", "batteries": "batteries"}
_GEO_KIND: dict[str, Any] = {"global": "global", "asia": "region", "india": "country"}


def _tokens(body: str, prefix: str) -> list[str]:
    return [token[len(prefix) :] for token in body.split() if token.startswith(prefix)]


@dataclass
class FakeAnalyzer:
    """Parses ``market:`` / ``geo:`` / ``type:`` tokens; can be told to fail for a URL."""

    fail_for: set[str] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)

    def __call__(self, item: Any, *, taxonomy: TaxonomyPolicy) -> PageAnalysis:
        self.calls.append(item.url)
        if item.url in self.fail_for:
            raise RuntimeError("scripted analyser failure with secret postgresql://ken:ken@x")
        markets = _tokens(item.body_text, "market:")
        geos = _tokens(item.body_text, "geo:")
        types = _tokens(item.body_text, "type:")
        market = markets[0] if markets else None
        return PageAnalysis(
            page_id=item.page_id,
            page_version=item.page_version,
            analysis_version=analysis_version(taxonomy.version),
            page_type=types[0] if types else "report",
            intent=Intent.COMMERCIAL_RESEARCH,
            commercial_importance=2,
            industry_id=_ROOTS.get(market) if market else None,
            market_id=market,
            topics=tuple(
                TopicScore(node_id=m, label=m, score=1.0, depth=0 if _ROOTS[m] == m else 1)
                for m in markets
            ),
            geographies=tuple(
                GeographyMention(geo_id=g, label=g, kind=_GEO_KIND[g], score=1.0) for g in geos
            ),
            keywords=tuple(sorted({w for w in item.body_text.split() if ":" not in w})[:5]),
            summary=item.body_text[:100],
        )


@dataclass(frozen=True)
class Candidate:
    relationship_type: RelationshipType
    weight: float
    evidence: dict[str, Any]


@dataclass(frozen=True)
class Match:
    rule_id: str
    reason: str
    source: ProtectionSource


def fake_match_protections(
    *, url: str, metadata: Mapping[str, str], taxonomy: TaxonomyPolicy
) -> tuple[Match, ...]:
    return tuple(
        Match(rule_id=rule.id, reason=rule.reason, source=ProtectionSource.POLICY)
        for rule in taxonomy.protections
        if rule.metadata_equals
        and all(metadata.get(k) == v for k, v in rule.metadata_equals.items())
    )


def fake_taxonomy_relationships(
    source: PageAnalysis, target: PageAnalysis, *, taxonomy: TaxonomyPolicy
) -> tuple[Candidate, ...]:
    out: list[Candidate] = []
    if source.market_id is not None and source.market_id == target.market_id:
        out.append(Candidate(RelationshipType.SAME_MARKET, 1.0, {"market": source.market_id}))
    shared = sorted({g.geo_id for g in source.geographies} & {g.geo_id for g in target.geographies})
    if shared:
        out.append(Candidate(RelationshipType.SAME_GEOGRAPHY, 0.4, {"shared": shared}))
    return tuple(out)


def fake_semantic_relationship(score: float, *, min_score: float) -> Candidate | None:
    if math.isnan(score) or score <= min_score:
        return None
    return Candidate(
        RelationshipType.SEMANTIC_SIMILAR, min(1.0, max(0.0, score)), {"cosine": round(score, 4)}
    )


def fake_recipe_text(
    *,
    title: str | None,
    meta_description: str | None,
    headings: Sequence[str],
    body_text: str,
    max_chars: int,
    prefix: str,
) -> str:
    return (prefix + " ".join(p for p in (title or "", body_text) if p))[:max_chars]


def everything_relates(
    analysis: PageAnalysis, taxonomy: TaxonomyPolicy
) -> tuple[frozenset[str], frozenset[str]]:
    return frozenset({"*"}), frozenset({"*"})


def fake_toolkit(analyzer: FakeAnalyzer | None = None) -> AnalysisToolkit:
    return AnalysisToolkit(
        analyze=analyzer or FakeAnalyzer(),
        make_input=lambda **fields: SimpleNamespace(**fields),
        match_protections=fake_match_protections,
        taxonomy_relationships=fake_taxonomy_relationships,
        semantic_relationship=fake_semantic_relationship,
        recipe=EmbeddingRecipe(
            text=fake_recipe_text, digest=lambda t: hashlib.sha256(t.encode()).hexdigest()
        ),
        relationship_keys=everything_relates,
    )


# --------------------------------------------------------------------------- #
# Embedding provider
# --------------------------------------------------------------------------- #
def bag_of_words(text_: str) -> list[float]:
    words = text_.lower().split()
    return [float(words.count(term)) + 0.01 for term in VOCABULARY]


class FakeEmbedder:
    """Satisfies ``ModelProvider``; embeddings are vocabulary counts (deterministic)."""

    name = "fake-embedder"

    def __init__(self) -> None:
        self.capability_model: str | None = EMBED_MODEL
        self.result_model = EMBED_MODEL
        self.fail_with: BaseException | None = None
        self.capabilities_error: Exception | None = None
        self.fail_on_calls: set[int] = set()
        self.embed_calls: list[list[str]] = []

    async def complete(
        self,
        *,
        role: ModelRole,
        messages: Sequence[Message],
        options: GenerationOptions | None = None,
    ) -> Completion:
        raise NotImplementedError

    async def complete_structured(  # type: ignore[no-untyped-def]
        self, *, role, messages, schema, options=None, max_attempts=2
    ) -> StructuredResult[Any]:
        raise NotImplementedError

    async def embed(
        self, *, texts: Sequence[str], role: ModelRole = ModelRole.EMBEDDING
    ) -> EmbeddingResult:
        self.embed_calls.append(list(texts))
        if self.fail_with is not None:
            raise self.fail_with
        if len(self.embed_calls) in self.fail_on_calls:
            from ken_contracts.model import ModelTimeoutError

            raise ModelTimeoutError("scripted timeout")
        return EmbeddingResult(
            vectors=[bag_of_words(t) for t in texts],
            model=self.result_model,
            dimensions=len(VOCABULARY),
            latency_ms=0.0,
            provider=self.name,
        )

    async def capabilities(self) -> ProviderCapabilities:
        if self.capabilities_error is not None:
            raise self.capabilities_error
        return ProviderCapabilities(
            provider=self.name,
            supports_tools=False,
            supports_json_schema=False,
            supports_embeddings=True,
            embedding_dimensions=len(VOCABULARY),
            roles=[
                ModelRoleStatus(
                    role=ModelRole.EMBEDDING,
                    model=self.capability_model,
                    status="ok" if self.capability_model else "missing",
                )
            ],
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider=self.name, reachable=True)

    async def aclose(self) -> None:
        return None


# --------------------------------------------------------------------------- #
# Seeding and running
# --------------------------------------------------------------------------- #
def page(path: str, body: str, *, metadata: dict[str, str] | None = None, **kwargs: Any) -> Any:
    built = make_page(path, body=body, **kwargs)
    if metadata:
        built = built.model_copy(update={"metadata": metadata})
    return built


async def seed(sessionmaker: async_sessionmaker[AsyncSession], *pages: Any) -> IngestionRunDetail:
    return await ingest(sessionmaker, *pages)


async def analyse(
    sessionmaker: async_sessionmaker[AsyncSession],
    provider: Any | None = None,
    *,
    analyzer: FakeAnalyzer | None = None,
    settings: Any = None,
    **kwargs: Any,
) -> JobRunDetail:
    return await run_analysis(
        sessionmaker=sessionmaker,
        settings=settings
        or make_settings(similarity_top_k=3, similarity_min_score=0.5, embedding_batch_size=2),
        model_provider=provider if provider is not None else FakeEmbedder(),
        triggered_by="test",
        taxonomy=TAXONOMY,
        toolkit=fake_toolkit(analyzer),
        clock=TickClock(),
        **kwargs,
    )


async def page_ids(sessionmaker: async_sessionmaker[AsyncSession]) -> dict[str, uuid.UUID]:
    """Path -> page id."""
    async with sessionmaker() as session:
        rows = (await session.execute(text("SELECT canonical_url, id FROM pages"))).all()
    return {str(url).removeprefix("https://site.test"): uuid.UUID(str(pid)) for url, pid in rows}


async def scalar(sessionmaker: async_sessionmaker[AsyncSession], sql: str, **params: Any) -> Any:
    async with sessionmaker() as session:
        return await session.scalar(text(sql), params)


async def rows(
    sessionmaker: async_sessionmaker[AsyncSession], sql: str, **params: Any
) -> list[Any]:
    async with sessionmaker() as session:
        return list((await session.execute(text(sql), params)).all())


async def edges(
    sessionmaker: async_sessionmaker[AsyncSession], kind: str | None = None
) -> set[tuple[str, str, str]]:
    """(source path, target path, type) of every stored edge."""
    ids = {pid: path for path, pid in (await page_ids(sessionmaker)).items()}
    found = await rows(
        sessionmaker,
        "SELECT source_page_id, target_page_id, relationship_type FROM page_relationships",
    )
    return {(ids[s], ids[t], k) for s, t, k in found if kind is None or k == kind}
