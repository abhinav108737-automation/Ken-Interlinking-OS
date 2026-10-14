"""Recommendation engine contracts (Phase 3).

FROZEN CONTRACT. Implements IMPLEMENTATION_PLAN.md Phase 3 / PRD v1.1 FR-005..FR-008,
section 12.2-12.4 and PRD section 7.8 (LinkPlan).

Pipeline (deterministic; no LLM in Phase 3)::

    eligible sources --(channels: semantic | lexical | taxonomy | graph)--> candidates
      --eligibility filters (reason codes)--> retained candidates
      --scoring (versioned ranking policy, component breakdown)--> ranked
      --diversity + per-source caps--> selected
      --placement (natural anchor in an existing block; never fabricated)--> LinkPlan

A ``LinkPlan`` is the unit a reviewer approves (Phase 5) and the deployment
service applies (Phase 6). Its identifying content is IMMUTABLE once proposed
(a database trigger rejects any UPDATE that touches a content column); only
``status`` moves, and every move is an append-only transition record. Anything
that would change the plan's content (an edited anchor, a replaced destination)
creates a NEW plan that ``supersedes`` the old one, so what a reviewer approved
is exactly what gets deployed.

Design: docs/adr/ADR-0011-recommendation-engine.md. Thresholds, caps,
compatibility matrices and anchor rules are POLICY DATA
(policies/recommendation/recommendation.v<N>.yaml); component weights are the
TRD section 12 ranking policy (policies/ranking/ranking_weights.v<N>.yaml).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Final, Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from ken_contracts.common import FrozenModel
from ken_contracts.errors import ListResponse
from ken_contracts.intelligence import Intent, RelationshipType

ENGINE_VERSION: Final = "deterministic-engine-1"
"""Recorded on every plan as its producer (PRD 7.8 'agent version')."""
PLACEMENT_ALGORITHM_VERSION: Final = 1
SUBJECT_LINK_PLAN: Final = "link_plan"


class RetrievalChannel(StrEnum):
    SEMANTIC = "semantic"
    LEXICAL = "lexical"
    TAXONOMY = "taxonomy"
    GRAPH = "graph"
    """Link-graph opportunity: under-linked / orphan targets."""


class CandidateRejection(StrEnum):
    """Deterministic eligibility filters (FR-006). Ordered; first match is recorded."""

    SELF_LINK = "self_link"
    SOURCE_INELIGIBLE = "source_ineligible"
    TARGET_INELIGIBLE = "target_ineligible"
    TARGET_PROTECTED = "target_protected"
    ALREADY_LINKED = "already_linked"
    DUPLICATE_RECOMMENDATION = "duplicate_recommendation"
    PAGE_TYPE_INCOMPATIBLE = "page_type_incompatible"
    LINK_DENSITY_EXCEEDED = "link_density_exceeded"
    BELOW_MIN_SCORE = "below_min_score"
    DIVERSITY_LIMIT = "diversity_limit"
    PER_SOURCE_LIMIT = "per_source_limit"
    NO_NATURAL_ANCHOR = "no_natural_anchor"


class ReasonCode(StrEnum):
    """Why a plan exists: structured, explainable, never free-form model text."""

    SEMANTICALLY_SIMILAR = "semantically_similar"
    LEXICAL_OVERLAP = "lexical_overlap"
    SAME_MARKET = "same_market"
    PARENT_MARKET = "parent_market"
    SUBMARKET = "submarket"
    RELATED_MARKET = "related_market"
    SUPPLY_CHAIN = "supply_chain"
    COMPLEMENTARY_MARKET = "complementary_market"
    INDUSTRY_ADJACENCY = "industry_adjacency"
    SAME_GEOGRAPHY = "same_geography"
    REGIONAL_CONTEXT = "regional_context"
    HIGH_COMMERCIAL_VALUE = "high_commercial_value"
    UNDERLINKED_TARGET = "underlinked_target"
    ORPHAN_TARGET = "orphan_target"
    ANCHOR_MATCHES_TARGET_TOPIC = "anchor_matches_target_topic"
    ANCHOR_MATCHES_TARGET_TITLE = "anchor_matches_target_title"


class LinkPlanStatus(StrEnum):
    """PRD v1.1 Appendix C recommendation lifecycle."""

    PROPOSED = "proposed"
    CRITIQUED = "critiqued"
    POLICY_CHECKED = "policy_checked"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    """Replaced by an edited plan (EDITED in the PRD lifecycle)."""
    VALIDATED = "validated"
    DEPLOYED = "deployed"
    VERIFIED = "verified"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    EXPIRED = "expired"
    """The source page changed before deployment: the plan is stale and must be regenerated."""


class ScoreComponent(FrozenModel):
    """One weighted component of the transparent score (FR-007)."""

    name: str = Field(description="Ranking-policy weight key, e.g. 'semantic'.")
    value: float = Field(ge=0.0, le=1.0, description="Normalised component value.")
    weight: float = Field(ge=0.0)
    contribution: float = Field(ge=0.0, description="value x weight.")


class ScoreBreakdown(FrozenModel):
    components: tuple[ScoreComponent, ...]
    penalties: dict[str, float] = Field(default_factory=dict)
    total: float = Field(ge=0.0, le=1.0)
    policy_name: str
    policy_version: int


# --------------------------------------------------------------------------- #
# Policy data
# --------------------------------------------------------------------------- #
RANKING_COMPONENTS: Final = (
    "semantic",
    "taxonomy",
    "intent",
    "graph",
    "geography",
    "commercial",
    "underlinked",
    "historical",
)
"""TRD section 12 component names, in order. The ranking policy must weight exactly these."""


class RankingPolicy(FrozenModel):
    """``policies/ranking/ranking_weights.v<N>.yaml`` (TRD section 12)."""

    policy: Literal["ranking"]
    version: int = Field(ge=1)
    status: str
    source: str
    weights: dict[str, float]

    @field_validator("weights")
    @classmethod
    def _weights_complete(cls, value: dict[str, float]) -> dict[str, float]:
        if set(value) != set(RANKING_COMPONENTS):
            raise ValueError(f"weights must name exactly {list(RANKING_COMPONENTS)}")
        if any(weight < 0 for weight in value.values()):
            raise ValueError("weights must be non-negative")
        if abs(sum(value.values()) - 1.0) > 1e-6:
            raise ValueError("weights must sum to 1")
        return value


class RetrievalPolicy(FrozenModel):
    semantic_k: int = Field(default=30, ge=0, le=500)
    lexical_k: int = Field(default=30, ge=0, le=500)
    graph_k: int = Field(default=20, ge=0, le=500)
    max_candidates_per_source: int = Field(default=80, ge=1, le=2000)
    channels_enabled: tuple[RetrievalChannel, ...] = Field(
        default=(
            RetrievalChannel.SEMANTIC,
            RetrievalChannel.LEXICAL,
            RetrievalChannel.TAXONOMY,
            RetrievalChannel.GRAPH,
        ),
        description="Each channel can be ablated independently (PRD v1.1 section 12.2).",
    )


class SelectionPolicy(FrozenModel):
    min_score: float = Field(default=0.35, ge=0.0, le=1.0)
    max_plans_per_source: int = Field(default=3, ge=1, le=50)
    max_plans_per_target_per_run: int = Field(default=5, ge=1, le=1000)
    max_internal_links_per_100_words: float = Field(
        default=2.0, gt=0.0, description="Existing + proposed internal body links, per 100 words."
    )
    max_links_per_block: int = Field(
        default=1, ge=1, le=10, description="Existing + proposed links in one block."
    )


class PlacementPolicy(FrozenModel):
    block_kinds: tuple[str, ...] = Field(
        default=("p", "li"),
        description="Never headings, tables, captions, quotes, code (PRD v1.1 section 12.4).",
    )
    min_anchor_words: int = Field(default=1, ge=1, le=10)
    max_anchor_words: int = Field(default=8, ge=1, le=20)
    min_anchor_chars: int = Field(default=4, ge=1, le=50)
    forbidden_anchors: tuple[str, ...] = Field(
        default=("click here", "here", "read more", "learn more", "this page", "this report")
    )
    skip_first_block: bool = Field(
        default=False, description="Avoid placing in the opening paragraph (editorial choice)."
    )


class RecommendationPolicy(FrozenModel):
    """``policies/recommendation/recommendation.v<N>.yaml``. Validated on load."""

    policy: Literal["recommendation"]
    version: int = Field(ge=1)
    status: str
    source: str
    ranking_policy: str = Field(description="Path of the ranking weights file, repo-relative.")
    retrieval: RetrievalPolicy = Field(default_factory=RetrievalPolicy)
    selection: SelectionPolicy = Field(default_factory=SelectionPolicy)
    placement: PlacementPolicy = Field(default_factory=PlacementPolicy)
    intent_compatibility: dict[Intent, dict[Intent, float]] = Field(
        description="source intent -> target intent -> score in [0,1]; missing pairs score 0."
    )
    page_type_targets: dict[str, tuple[str, ...]] = Field(
        description="source page type -> page types it may link to (others are rejected)."
    )
    underlinked_inbound_threshold: int = Field(
        default=3, ge=1, description="Fewer current inbound internal links than this = underlinked."
    )
    geography_scores: dict[str, float] = Field(
        default_factory=lambda: {
            "same_country": 1.0,
            "region_contains_source": 0.6,
            "same_region": 0.5,
            "global_target": 0.3,
        }
    )

    @model_validator(mode="after")
    def _scores_in_range(self) -> RecommendationPolicy:
        for row in self.intent_compatibility.values():
            for value in row.values():
                if not 0.0 <= value <= 1.0:
                    raise ValueError("intent compatibility scores must be in [0, 1]")
        for value in self.geography_scores.values():
            if not 0.0 <= value <= 1.0:
                raise ValueError("geography scores must be in [0, 1]")
        return self


# --------------------------------------------------------------------------- #
# Placement
# --------------------------------------------------------------------------- #
class PlacementLocator(FrozenModel):
    """Where the anchor goes. Mirrors ``ken_contracts.cms.LinkLocator`` plus offsets.

    ``block_id``/``block_kind`` come from the deterministic block walk of the
    SOURCE VERSION's stored body (Phase 1 ``extract_blocks``); ``start``/``end``
    are character offsets into that block's text; ``occurrence_index`` is which
    occurrence of ``anchor_text`` in the block. ``context_hash`` is the sha256
    of the full block text, so Phase 6 detects drift before mutating.
    """

    block_id: str
    block_kind: str
    occurrence_index: int = Field(ge=0)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    context_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class PlacementPreview(FrozenModel):
    before: str = Field(description="Block text before the anchor (trimmed to ~200 chars).")
    anchor: str
    after: str = Field(description="Block text after the anchor (trimmed to ~200 chars).")


class PlanFreshness(StrEnum):
    """Whether a plan still applies to the source page as it is NOW (Phase 6 checks this)."""

    FRESH = "fresh"
    """Source version unchanged."""
    REBASABLE = "rebasable"
    """Version changed, but the block text (context hash) and the anchor offsets are intact."""
    STALE = "stale"
    """The block is gone or changed, or the anchor moved: the plan must be regenerated."""


class FreshnessCheck(FrozenModel):
    freshness: PlanFreshness
    reasons: tuple[str, ...] = Field(
        default=(), description="version_changed | block_missing | context_changed | anchor_moved"
    )


class LinkPlan(FrozenModel):
    """A proposed contextual link (PRD 7.8). Content immutable; status in transitions."""

    id: UUID
    run_id: UUID | None = None
    source_page_id: UUID
    source_page_version: int = Field(ge=1)
    source_url: str
    target_page_id: UUID
    target_page_version: int = Field(ge=1)
    target_url: str
    target_title: str | None = None
    relationship_type: RelationshipType | None = None
    channels: tuple[RetrievalChannel, ...]
    anchor_text: str = Field(min_length=1, max_length=120)
    anchor_source: Literal["target_topic", "target_title", "target_keyword", "edited"]
    locator: PlacementLocator
    preview: PlacementPreview
    score: ScoreBreakdown
    confidence: float = Field(
        ge=0.0, le=1.0, description="Raw in Phase 3; calibrated from Phase 8."
    )
    reason_codes: tuple[ReasonCode, ...]
    rationale: str = Field(description="Deterministic sentence(s) rendered from reason codes.")
    engine_version: str
    policy_version: int = Field(ge=1, description="Recommendation policy version.")
    ranking_policy_version: int = Field(ge=1)
    embedding_model: str | None = Field(
        default=None, description="Provider-reported identity used."
    )
    analysis_version: str
    supersedes: UUID | None = None
    status: LinkPlanStatus
    created_at: AwareDatetime
    updated_at: AwareDatetime


class CandidateView(FrozenModel):
    """A retrieved candidate, kept or rejected, for explainability and evals."""

    run_id: UUID
    source_page_id: UUID
    target_page_id: UUID
    target_url: str
    channels: tuple[RetrievalChannel, ...]
    channel_scores: dict[str, float]
    total_score: float | None = None
    rejection: CandidateRejection | None = None
    plan_id: UUID | None = None


class PlanTransitionView(FrozenModel):
    id: int
    plan_id: UUID
    from_status: LinkPlanStatus | None = None
    to_status: LinkPlanStatus
    actor: str
    note: str | None = Field(default=None, max_length=500)
    occurred_at: AwareDatetime


class LinkPlanDetail(LinkPlan):
    transitions: list[PlanTransitionView]
    source_title: str | None = None
    source_block_text: str = Field(description="Full block text at the source version.")


class OpportunityKind(StrEnum):
    ORPHAN = "orphan"
    """No internal inbound links from any current page."""
    UNDERLINKED = "underlinked"
    """Commercially important page with fewer inbound links than the policy threshold."""
    LINK_CAPACITY = "link_capacity"
    """Eligible source with room for more contextual links."""


class OpportunityView(FrozenModel):
    page_id: UUID
    canonical_url: str
    title: str | None = None
    kind: OpportunityKind
    score: float = Field(ge=0.0, le=1.0)
    evidence: dict[str, Any]
    run_id: UUID
    computed_at: AwareDatetime


class LinkPlanEvent(FrozenModel):
    """Payload of ``link.proposed`` (and later link.* lifecycle events)."""

    plan_id: UUID
    source_page_id: UUID
    source_page_version: int
    target_page_id: UUID
    anchor_text: str
    status: LinkPlanStatus
    score: float
    run_id: UUID | None = None
    actor: str


class RecommendationSummary(FrozenModel):
    """Counts for the dashboard."""

    plans_by_status: dict[str, int]
    sources_with_plans: int = Field(ge=0)
    targets_with_plans: int = Field(ge=0)
    opportunities_by_kind: dict[str, int]
    last_run_id: UUID | None = None
    policy_version: int
    ranking_policy_version: int


class PlanNotFoundError(Exception):
    def __init__(self) -> None:
        super().__init__("link plan not found")


@runtime_checkable
class RecommendationReader(Protocol):
    async def list_plans(
        self,
        *,
        limit: int,
        offset: int,
        status: LinkPlanStatus | None = None,
        source_page_id: UUID | None = None,
        target_page_id: UUID | None = None,
        min_score: float | None = None,
    ) -> ListResponse[LinkPlan]: ...

    async def get_plan(self, *, plan_id: UUID) -> LinkPlanDetail: ...

    async def list_candidates(
        self,
        *,
        limit: int,
        offset: int,
        run_id: UUID | None = None,
        source_page_id: UUID | None = None,
        rejected: bool | None = None,
    ) -> ListResponse[CandidateView]: ...

    async def list_opportunities(
        self, *, limit: int, offset: int, kind: OpportunityKind | None = None
    ) -> ListResponse[OpportunityView]: ...

    async def get_summary(self) -> RecommendationSummary: ...
