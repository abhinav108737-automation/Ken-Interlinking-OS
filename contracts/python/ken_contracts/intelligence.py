"""Content intelligence contracts (Phase 2).

FROZEN CONTRACT. Implements IMPLEMENTATION_PLAN.md Phase 2 / PRD v1.1 section 12.1
and FR-004: taxonomy, intent, entities, topics, summaries/salient blocks,
embeddings, page relationships, protection flags and link eligibility.

Deterministic first (TRD.md section 11). Every analysis field in this module is
produced by code from a VERSIONED taxonomy policy (policies/taxonomy/), not by
an LLM. Phase 4's understanding agent may later add LLM-derived annotations,
which will be marked ``extractor="llm"`` and validated through
``complete_structured``.

Four groups:

1. **Taxonomy policy** -- the schema of ``policies/taxonomy/taxonomy.v<N>.yaml``:
   market tree, geography tree, entity gazetteer, page-type rules, protection
   rules and eligibility thresholds. Data, not code: changing it means a new
   version file and a new ``analysis_version``.
2. **Analysis output** -- :class:`PageAnalysis`, one per (page version,
   analysis version). Immutable; a newer version is a new row.
3. **Embeddings and relationships** -- page-level vectors whose dimension comes
   from the provider at runtime (never a constant), and typed page-to-page
   relationships (PRD section 7.3) with evidence.
4. **Read side** -- API views and :class:`IntelligenceReader`.

Eligibility is computed at READ time from current state (publication, gone,
protection, robots, canonical, analysis), never stored, so it cannot go stale.

Embedding identity (freshness and similarity key) is the PROVIDER-REPORTED
model -- ``ProviderCapabilities.roles[EMBEDDING].model`` /
``EmbeddingResult.model`` -- together with ``provider``, ``dimensions``,
``recipe_version`` and ``recipe_fingerprint`` (a hash of the recipe version and
the settings that shape the embedded text, document prefix and max chars, so
changing either makes every stored embedding stale); never the raw
``KEN_MODEL_EMBEDDING`` setting string. Every provider must report the same
model string in both places (a contract test checks the mock and Ollama
providers), and one shared helper
(``ken_services.intelligence.embeddings.embedding_identity``) resolves it.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Final, Literal, Protocol, Self, runtime_checkable
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from ken_contracts.common import FrozenModel
from ken_contracts.errors import ListResponse

ANALYSIS_ALGORITHM_VERSION: Final = 1
"""Version of the deterministic analysis code. Bump on any behaviour change."""

EMBEDDING_RECIPE_VERSION: Final = 1
"""Version of the text recipe that is embedded per page. Bump on any change."""

_ID_PATTERN: Final = r"^[a-z0-9][a-z0-9_.-]{0,99}$"


def analysis_version(taxonomy_version: int) -> str:
    """The version string stored with every analysis: taxonomy + algorithm."""
    return f"tax{taxonomy_version}-alg{ANALYSIS_ALGORITHM_VERSION}"


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #
class Intent(StrEnum):
    """Reader/search intent of a page (PRD section 7.2)."""

    COMMERCIAL_RESEARCH = "commercial_research"
    """A report or data product a reader may buy or request."""
    INFORMATIONAL = "informational"
    """Insight, article, explainer."""
    NAVIGATIONAL = "navigational"
    """Listing, category or hub page."""
    TRANSACTIONAL = "transactional"
    """Contact, enquiry, pricing, checkout."""
    CORPORATE = "corporate"
    """About, careers, legal."""
    UNKNOWN = "unknown"


class RelationshipType(StrEnum):
    """Explainable page relationships (PRD section 7.3) plus semantic similarity."""

    SAME_MARKET = "same_market"
    PARENT_MARKET = "parent_market"
    """The target covers the source's parent market."""
    SUBMARKET = "submarket"
    """The target covers a child market of the source's market."""
    RELATED_MARKET = "related_market"
    """Sibling markets under the same parent."""
    SAME_GEOGRAPHY = "same_geography"
    REGIONAL_PARENT = "regional_parent"
    """The target covers a region containing the source's country."""
    SUPPLY_CHAIN_UPSTREAM = "supply_chain_upstream"
    SUPPLY_CHAIN_DOWNSTREAM = "supply_chain_downstream"
    COMPLEMENTARY_MARKET = "complementary_market"
    INDUSTRY_ADJACENCY = "industry_adjacency"
    SEMANTIC_SIMILAR = "semantic_similar"
    """Embedding cosine similarity above the configured threshold."""


class EntityType(StrEnum):
    COMPANY = "company"
    TECHNOLOGY = "technology"
    PRODUCT = "product"
    ORGANIZATION = "organization"


class ProtectionSource(StrEnum):
    """Who declared a page protected. Any active protection blocks every mutation."""

    POLICY = "policy"
    """A rule in the taxonomy policy (path pattern or metadata match)."""
    CMS = "cms"
    """The CMS itself flags the page protected."""
    MANUAL = "manual"
    """An operator (Phase 5 UI / CLI)."""


class IneligibilityReason(StrEnum):
    """Why a page cannot be a link source or target right now."""

    NOT_PUBLISHED = "not_published"
    GONE = "gone"
    PROTECTED = "protected"
    NOINDEX = "noindex"
    NOT_CANONICAL = "not_canonical"
    """The page's current version declared another URL canonical."""
    HTTP_NOT_OK = "http_not_ok"
    THIN_CONTENT = "thin_content"
    PAGE_TYPE_EXCLUDED = "page_type_excluded"
    NOT_ANALYZED = "not_analyzed"
    STALE_ANALYSIS = "stale_analysis"


# --------------------------------------------------------------------------- #
# Taxonomy policy (policies/taxonomy/taxonomy.v<N>.yaml)
# --------------------------------------------------------------------------- #
class MarketNode(FrozenModel):
    """A node of the market tree. Top-level nodes are industries."""

    id: str = Field(pattern=_ID_PATTERN)
    label: str = Field(min_length=1)
    parent: str | None = None
    aliases: tuple[str, ...] = Field(
        default=(), description="Phrases that mean this market (matched case-insensitively)."
    )
    adjacent: tuple[str, ...] = Field(default=(), description="INDUSTRY_ADJACENCY targets.")
    complementary: tuple[str, ...] = Field(default=(), description="COMPLEMENTARY_MARKET targets.")
    upstream: tuple[str, ...] = Field(default=(), description="SUPPLY_CHAIN_UPSTREAM markets.")
    downstream: tuple[str, ...] = Field(default=(), description="SUPPLY_CHAIN_DOWNSTREAM markets.")


class GeographyNode(FrozenModel):
    id: str = Field(pattern=_ID_PATTERN)
    label: str = Field(min_length=1)
    kind: Literal["global", "region", "country"]
    parent: str | None = None
    aliases: tuple[str, ...] = ()


class EntityEntry(FrozenModel):
    """Gazetteer entry: a named company, technology, product or organisation."""

    id: str = Field(pattern=_ID_PATTERN)
    label: str = Field(min_length=1)
    type: EntityType
    aliases: tuple[str, ...] = ()
    markets: tuple[str, ...] = Field(default=(), description="Markets the entity belongs to.")


class PageTypeRule(FrozenModel):
    """Classifies a page by its URL path. The first matching rule wins."""

    id: str = Field(pattern=_ID_PATTERN)
    label: str
    path_patterns: tuple[str, ...] = Field(description="Python regexes matched against the path.")
    intent: Intent
    commercial_importance: int = Field(ge=0, le=3, description="0 none ... 3 highest.")
    linkable_as_source: bool = True
    linkable_as_target: bool = True

    @field_validator("path_patterns")
    @classmethod
    def _patterns_compile(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in value:
            re.compile(pattern)
        return value


class ProtectionRule(FrozenModel):
    """Declares pages protected.

    A page matches when ANY path regex matches its URL path, OR when ALL
    ``metadata_equals`` pairs equal the page's metadata (conditions combine
    with OR; metadata pairs combine with AND).
    """

    id: str = Field(pattern=_ID_PATTERN)
    reason: str = Field(min_length=1)
    path_patterns: tuple[str, ...] = ()
    metadata_equals: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _has_a_condition(self) -> Self:
        if not self.path_patterns and not self.metadata_equals:
            raise ValueError("a protection rule needs path_patterns or metadata_equals")
        for pattern in self.path_patterns:
            re.compile(pattern)
        return self


class EligibilityPolicy(FrozenModel):
    min_words_source: int = Field(default=150, ge=0)
    min_words_target: int = Field(default=100, ge=0)


class TaxonomyPolicy(FrozenModel):
    """The validated taxonomy policy file. Referential integrity is checked here."""

    policy: Literal["taxonomy"]
    version: int = Field(ge=1)
    status: str
    source: str = Field(description="Provenance, e.g. 'fixture' until Ken supplies its taxonomy.")
    markets: tuple[MarketNode, ...]
    geographies: tuple[GeographyNode, ...]
    entities: tuple[EntityEntry, ...] = ()
    page_types: tuple[PageTypeRule, ...]
    default_page_type: str
    protections: tuple[ProtectionRule, ...] = ()
    eligibility: EligibilityPolicy = Field(default_factory=EligibilityPolicy)

    @model_validator(mode="after")
    def _references_resolve(self) -> Self:
        def unique(ids: list[str], what: str) -> set[str]:
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate {what} id")
            return set(ids)

        markets = unique([m.id for m in self.markets], "market")
        geos = unique([g.id for g in self.geographies], "geography")
        unique([e.id for e in self.entities], "entity")
        page_types = unique([p.id for p in self.page_types], "page type")
        unique([r.id for r in self.protections], "protection rule")
        for market in self.markets:
            refs = (market.parent, *market.adjacent, *market.complementary)
            for ref in (*refs, *market.upstream, *market.downstream):
                if ref is not None and ref not in markets:
                    raise ValueError(f"market {market.id!r} references unknown market {ref!r}")
        for geo in self.geographies:
            if geo.parent is not None and geo.parent not in geos:
                raise ValueError(f"geography {geo.id!r} has unknown parent {geo.parent!r}")
        for entity in self.entities:
            for ref in entity.markets:
                if ref not in markets:
                    raise ValueError(f"entity {entity.id!r} references unknown market {ref!r}")
        if self.default_page_type not in page_types:
            raise ValueError("default_page_type must be one of page_types")
        for tree, what in (
            ({m.id: m.parent for m in self.markets}, "market"),
            ({g.id: g.parent for g in self.geographies}, "geography"),
        ):
            for start in tree:
                seen: set[str] = set()
                node: str | None = start
                while node is not None:
                    if node in seen:
                        raise ValueError(f"{what} tree has a parent cycle through {start!r}")
                    seen.add(node)
                    node = tree.get(node)
        return self


# --------------------------------------------------------------------------- #
# Analysis output (one per page version x analysis version; immutable)
# --------------------------------------------------------------------------- #
class TopicScore(FrozenModel):
    node_id: str
    label: str
    score: float = Field(ge=0.0, description="Location-weighted mention score.")
    depth: int = Field(ge=0, description="0 = industry; deeper = more specific market.")


class GeographyMention(FrozenModel):
    geo_id: str
    label: str
    kind: Literal["global", "region", "country"]
    score: float = Field(ge=0.0)


class EntityMention(FrozenModel):
    entity_id: str
    label: str
    type: EntityType
    mentions: int = Field(ge=1)


class SalientBlock(FrozenModel):
    block_id: str
    score: float = Field(ge=0.0)
    text: str = Field(max_length=600)


class PageAnalysis(FrozenModel):
    """Deterministic understanding of one page version."""

    page_id: UUID
    page_version: int = Field(ge=1)
    analysis_version: str
    extractor: Literal["deterministic"] = "deterministic"
    page_type: str
    intent: Intent
    commercial_importance: int = Field(ge=0, le=3)
    industry_id: str | None = Field(default=None, description="Top-level market (industry).")
    market_id: str | None = Field(default=None, description="Most specific confident market.")
    topics: tuple[TopicScore, ...] = ()
    geographies: tuple[GeographyMention, ...] = ()
    entities: tuple[EntityMention, ...] = ()
    keywords: tuple[str, ...] = Field(default=(), description="Top salient terms, deterministic.")
    summary: str = Field(default="", max_length=600, description="Extractive, from page text.")
    salient_blocks: tuple[SalientBlock, ...] = ()


# --------------------------------------------------------------------------- #
# Embeddings, relationships, protections, eligibility
# --------------------------------------------------------------------------- #
class PageEmbeddingInfo(FrozenModel):
    """Metadata of a stored page embedding (the vector itself is never sent to clients)."""

    page_id: UUID
    page_version: int = Field(ge=1)
    model: str = Field(description="Configured model of the EMBEDDING role at write time.")
    provider: str
    dimensions: int = Field(gt=0, description="As reported by the provider, never assumed.")
    recipe_version: int = Field(ge=1)
    recipe_fingerprint: str = Field(
        min_length=1,
        description=(
            "Recipe version + the settings that shape the embedded text (document prefix, "
            "max chars), hashed. Part of the embedding identity."
        ),
    )
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: AwareDatetime


class PageRelationshipView(FrozenModel):
    source_page_id: UUID
    source_page_version: int = Field(ge=1, description="Source version the edge was computed from.")
    target_page_id: UUID
    target_page_version: int = Field(ge=1, description="Target version the edge was computed from.")
    target_canonical_url: str
    target_title: str | None = None
    relationship_type: RelationshipType
    weight: float = Field(ge=0.0, le=1.0)
    evidence: dict[str, Any] = Field(
        default_factory=dict, description="Why: shared node ids, cosine score, ... (no free text)."
    )
    analysis_version: str
    computed_at: AwareDatetime


class PageProtectionView(FrozenModel):
    id: int
    page_id: UUID
    source: ProtectionSource
    rule_id: str | None = None
    reason: str
    active: bool
    created_by: str
    created_at: AwareDatetime


class LinkEligibility(FrozenModel):
    """Whether a page may currently be a link source / target, and why not."""

    as_source: bool
    as_target: bool
    source_reasons: list[IneligibilityReason]
    target_reasons: list[IneligibilityReason]


class SimilarPage(FrozenModel):
    page_id: UUID
    canonical_url: str
    title: str | None = None
    score: float = Field(ge=-1.0, le=1.0, description="Cosine similarity.")
    page_type: str | None = None
    market_id: str | None = None


class PageIntelligenceView(FrozenModel):
    page_id: UUID
    canonical_url: str
    title: str | None = None
    current_version: int = Field(ge=1)
    analysis: PageAnalysis | None = Field(
        default=None, description="Latest analysis of the CURRENT version, if any."
    )
    analysis_fresh: bool = Field(
        description="An analysis exists for the current version and current analysis version."
    )
    embedding: PageEmbeddingInfo | None = None
    embedding_fresh: bool
    protections: list[PageProtectionView]
    eligibility: LinkEligibility
    relationship_counts: dict[str, int] = Field(default_factory=dict)


class TaxonomyNodeView(FrozenModel):
    id: str
    label: str
    kind: Literal["market", "global", "region", "country"]
    parent: str | None = None
    depth: int = Field(ge=0)
    page_count: int = Field(ge=0, description="Pages whose fresh analysis lists this node.")


class PageTypeView(FrozenModel):
    id: str
    label: str
    intent: Intent
    commercial_importance: int
    linkable_as_source: bool
    linkable_as_target: bool
    page_count: int = Field(ge=0)


class TaxonomyView(FrozenModel):
    version: int
    status: str
    source: str
    analysis_version: str
    markets: list[TaxonomyNodeView]
    geographies: list[TaxonomyNodeView]
    page_types: list[PageTypeView]


class IntelligenceCoverage(FrozenModel):
    total_pages: int = Field(ge=0)
    analysis_version: str
    analyzed_fresh: int = Field(ge=0)
    analyzed_stale: int = Field(ge=0)
    never_analyzed: int = Field(ge=0)
    embedding_model: str
    embedded_fresh: int = Field(ge=0)
    embedded_stale_or_missing: int = Field(ge=0)
    protected: int = Field(ge=0)
    eligible_sources: int = Field(ge=0)
    eligible_targets: int = Field(ge=0)
    relationships: int = Field(ge=0)
    by_page_type: dict[str, int] = Field(default_factory=dict)
    by_industry: dict[str, int] = Field(default_factory=dict)


class IntelligenceNotFoundError(Exception):
    def __init__(self, resource: Literal["page", "taxonomy_node"]) -> None:
        super().__init__(f"{resource} not found")
        self.resource = resource


class EmbeddingUnavailableError(Exception):
    """Semantic search needs the EMBEDDING role, and it is not available now."""


@runtime_checkable
class IntelligenceReader(Protocol):
    """Everything the intelligence API reads. Async, keyword-only, read-only."""

    async def get_page_intelligence(self, *, page_id: UUID) -> PageIntelligenceView: ...

    async def list_similar(self, *, page_id: UUID, limit: int) -> ListResponse[SimilarPage]:
        """Nearest pages by stored embedding (current versions, same model)."""
        ...

    async def list_relationships(
        self,
        *,
        page_id: UUID,
        limit: int,
        offset: int,
        relationship_type: RelationshipType | None = None,
    ) -> ListResponse[PageRelationshipView]: ...

    async def get_taxonomy(self) -> TaxonomyView: ...

    async def list_topic_pages(
        self, *, node_id: str, limit: int, offset: int
    ) -> ListResponse[SimilarPage]:
        """Pages whose fresh analysis lists ``node_id`` as a topic or geography (score = 1)."""
        ...

    async def get_coverage(self) -> IntelligenceCoverage: ...

    async def semantic_search(self, *, query: str, limit: int) -> ListResponse[SimilarPage]:
        """Embed ``query`` (EMBEDDING role) and return the nearest pages.

        Raises:
            EmbeddingUnavailableError: the embedding model cannot be reached.
        """
        ...
