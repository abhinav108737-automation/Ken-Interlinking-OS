"""Recommendation engine contracts (Phase 3).

FROZEN CONTRACT. Implements IMPLEMENTATION_PLAN.md Phase 3 / PRD v1.1 FR-005..FR-008,
section 12.2-12.4 and PRD section 7.8 (LinkPlan).

Pipeline (deterministic; no LLM in Phase 3)::

    eligible sources
      --(channels: semantic | lexical | taxonomy | graph | analytics | orphan_recovery)
      --> candidates
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

import math
import re
from collections.abc import Mapping
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
    """Vector retrieval through ``VectorStore.search`` (ADR-0017; pgvector is gone)."""
    LEXICAL = "lexical"
    TAXONOMY = "taxonomy"
    GRAPH = "graph"
    """Link-graph opportunity: under-linked / orphan targets."""
    ANALYTICS = "analytics"
    """Destinations surfaced by stored analytics (demand without supply, under-performing
    pages). **Analytics may SURFACE and may BLOCK; it may never alone JUSTIFY** (ADR-0016
    s16.7: first-party counts are spoofable). A candidate found only by this channel still
    needs a non-analytics relevance signal above the policy floor, and its contribution is
    recorded per signal in :class:`CandidateAttribution` -- never folded into one score."""
    ORPHAN_RECOVERY = "orphan_recovery"
    """Targets that ``page_graph_metrics`` marks ``is_orphan`` / ``is_nearly_orphan``: the
    explicit recovery pass, separate from the general GRAPH channel so its yield and its
    precision can be measured (and ablated) on their own."""


class CandidateRejection(StrEnum):
    """Deterministic filters (FR-006), in EVALUATION ORDER; the first match is recorded.

    Pair filters run first (self .. page type), then scoring (``below_min_relevance``),
    then placement in rank order (``no_natural_anchor``, ``below_min_score`` on the
    FINAL score), then selection caps (density, per-source, diversity).
    """

    SELF_LINK = "self_link"
    SOURCE_PROTECTED = "source_protected"
    TARGET_PROTECTED = "target_protected"
    EXPERIMENT_LOCKED = "experiment_locked"
    """Reserved (PRD 7.5): an active experiment forbids modifying the pair. Never fires before
    Phase 7 provides experiments; the hook is ``ExperimentGuard``."""
    SOURCE_INELIGIBLE = "source_ineligible"
    TARGET_INELIGIBLE = "target_ineligible"
    ALREADY_LINKED = "already_linked"
    DUPLICATE_RECOMMENDATION = "duplicate_recommendation"
    PREVIOUSLY_REJECTED = "previously_rejected"
    """A reviewer rejected this pair within ``selection.rejection_cooldown_days``."""
    TARGET_EXCLUDED = "target_excluded"
    """The target's URL path matches ``excluded_target_paths`` (e.g. the home page)."""
    PAGE_TYPE_INCOMPATIBLE = "page_type_incompatible"
    GEOGRAPHY_MISMATCH = "geography_mismatch"
    """The GEOGRAPHY GATE refused the pair: the target's PRIMARY geography is not the
    source's, is not a container of it, is not global, and (for the conditional rows of the
    gate) the source page text does not name it. A geography mismatch is a refusal, never a
    score penalty (``ken_services.recommendations.gates.geography_gate``)."""
    MARKET_UNRELATED = "market_unrelated"
    """The MARKET GATE refused the pair: the two markets are neither the same, nor in a
    taxonomy parent/child relation, nor in a declared supply-chain or complementary relation.
    Word overlap alone never relates two markets."""
    BELOW_MIN_RELEVANCE = "below_min_relevance"
    NO_MARKET_KEYWORD_ANCHOR = "no_market_keyword_anchor"
    """The anchor must be the TARGET's own market keyword (its specific market phrase plus
    "market"), and the source page text does not contain it verbatim. Nothing is rewritten
    here: the pair is simply not proposed."""
    NO_NATURAL_ANCHOR = "no_natural_anchor"
    BELOW_MIN_SCORE = "below_min_score"
    LINK_DENSITY_EXCEEDED = "link_density_exceeded"
    PER_SOURCE_LIMIT = "per_source_limit"
    DIVERSITY_LIMIT = "diversity_limit"


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
    GLOBAL_TARGET = "global_target"
    """The target carries no specific geography, so it serves every source geography."""
    CROSS_GEOGRAPHY_SUPPORTED = "cross_geography_supported"
    """The target's geography is NOT the source's, and the source page text names it: the
    only condition under which the geography gate allows a cross-geography link."""
    HIGH_COMMERCIAL_VALUE = "high_commercial_value"
    UNDERLINKED_TARGET = "underlinked_target"
    ORPHAN_TARGET = "orphan_target"
    ANCHOR_MATCHES_TARGET_TOPIC = "anchor_matches_target_topic"
    ANCHOR_MATCHES_TARGET_TITLE = "anchor_matches_target_title"
    ANCHOR_IS_TARGET_MARKET_KEYWORD = "anchor_is_target_market_keyword"
    """The anchor is the target's own market phrase plus "market" ("cosmetics market"),
    derived from the target's identity (canonical URL slug / title), not from the broad
    ``market_id`` analysis assigned."""
    ANCHOR_NAMES_TARGET_GEOGRAPHY = "anchor_names_target_geography"
    """The anchor also names the target's geography ("India cosmetics market"): required
    whenever the target's geography differs from the source's."""
    REGIONAL_SIBLING_MARKET = "regional_sibling_market"
    """The SAME market in a different geography -- "India EV market" from "Germany EV
    market". Distinct from ``CROSS_GEOGRAPHY_SUPPORTED``, which requires the source text to
    name the target's geography; a regional sibling needs no mention, because the anchor
    itself must name it (ADR-0027 §1 amendment, owner decision 2026-11-01). The owner's
    first priority: a reader researching one market wants the same market elsewhere."""


class LinkPlanStatus(StrEnum):
    """PRD v1.1 Appendix C recommendation lifecycle. Moves only along ``ALLOWED_TRANSITIONS``.

    ADR-0018 adds ``autonomy_decided`` and ``blocked`` and makes the approval pair
    EXCEPTIONAL. The normal autonomous path is::

        proposed -> critiqued -> policy_checked -> autonomy_decided -> validated
                 -> deployed -> verified -> measured -> learning_captured

    ``awaiting_approval`` / ``approved`` stay reachable, but only for a ``guarded``-mode
    deferral and for an operator acting on an exception. They are no longer on the
    normal path (ADR-0018 s3).
    """

    PROPOSED = "proposed"
    CRITIQUED = "critiqued"
    POLICY_CHECKED = "policy_checked"
    AUTONOMY_DECIDED = "autonomy_decided"
    """ADR-0018: an ``autonomy_decisions`` row exists and the decision is a publish. The
    plan is queued for the deployment service; no human has been, or will be, asked."""
    BLOCKED = "blocked"
    """ADR-0018: a deterministic hard block fired, or policy refused. Terminal UNLESS an
    operator overrides, which records who and why. Deliberately NOT in
    :data:`OPEN_STATUSES`: a blocked plan must not consume its target's diversity budget
    forever. The guard against re-proposing the same doomed pair is the per-(source, target)
    retry budget and cool-down in ``publish_attempts``
    (:class:`~ken_contracts.autonomy.RetryBudgetState`), not an occupied pair."""
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
    """The source changed so the plan is STALE (or the CMS answered 409): regenerate it."""
    MEASURED = "measured"
    """Phase 7: outcome metrics are attached."""
    LEARNING_CAPTURED = "learning_captured"
    """Phase 8: the outcome has been turned into learning data."""


_S = LinkPlanStatus
ALLOWED_TRANSITIONS: Final[Mapping[LinkPlanStatus, frozenset[LinkPlanStatus]]] = {
    _S.PROPOSED: frozenset(
        {
            _S.CRITIQUED,
            _S.POLICY_CHECKED,
            _S.AWAITING_APPROVAL,
            _S.BLOCKED,
            _S.REJECTED,
            _S.SUPERSEDED,
            _S.EXPIRED,
        }
    ),
    _S.CRITIQUED: frozenset(
        {
            _S.POLICY_CHECKED,
            _S.AWAITING_APPROVAL,
            _S.BLOCKED,
            _S.REJECTED,
            _S.SUPERSEDED,
            _S.EXPIRED,
        }
    ),
    _S.POLICY_CHECKED: frozenset(
        {
            _S.AUTONOMY_DECIDED,
            _S.AWAITING_APPROVAL,
            _S.BLOCKED,
            _S.REJECTED,
            _S.SUPERSEDED,
            _S.EXPIRED,
        }
    ),
    # The normal autonomous path: a publish decision hands the plan straight to the
    # deployment service. A guarded-mode deferral goes to awaiting_approval instead.
    _S.AUTONOMY_DECIDED: frozenset(
        {
            _S.VALIDATED,
            _S.AWAITING_APPROVAL,
            _S.BLOCKED,
            _S.REJECTED,
            _S.SUPERSEDED,
            _S.EXPIRED,
        }
    ),
    # Terminal unless an operator overrides: an override re-enters the autonomous path, and
    # the deterministic blocks are re-evaluated there (they are never waived).
    _S.BLOCKED: frozenset({_S.AUTONOMY_DECIDED, _S.SUPERSEDED, _S.EXPIRED}),
    _S.AWAITING_APPROVAL: frozenset(
        {_S.APPROVED, _S.BLOCKED, _S.REJECTED, _S.SUPERSEDED, _S.EXPIRED}
    ),
    _S.APPROVED: frozenset({_S.VALIDATED, _S.BLOCKED, _S.REJECTED, _S.SUPERSEDED, _S.EXPIRED}),
    _S.VALIDATED: frozenset({_S.DEPLOYED, _S.FAILED, _S.EXPIRED}),
    _S.DEPLOYED: frozenset({_S.VERIFIED, _S.FAILED, _S.ROLLED_BACK}),
    _S.VERIFIED: frozenset({_S.MEASURED, _S.ROLLED_BACK}),
    _S.FAILED: frozenset({_S.ROLLED_BACK}),
    _S.MEASURED: frozenset({_S.LEARNING_CAPTURED, _S.ROLLED_BACK}),
    _S.LEARNING_CAPTURED: frozenset({_S.ROLLED_BACK}),
    _S.REJECTED: frozenset(),
    _S.SUPERSEDED: frozenset(),
    _S.EXPIRED: frozenset(),
    _S.ROLLED_BACK: frozenset(),
}
"""The ONLY legal status moves, enforced by the single transition service (tested).

Terminal: rejected, superseded, expired, rolled_back. ``blocked`` is terminal except for an
operator override back onto the autonomous path. An approval is never resurrected: a rejected
or expired plan is regenerated as a NEW plan."""

OPEN_STATUSES: Final = frozenset(
    {
        _S.PROPOSED,
        _S.CRITIQUED,
        _S.POLICY_CHECKED,
        _S.AUTONOMY_DECIDED,
        _S.AWAITING_APPROVAL,
        _S.APPROVED,
        _S.VALIDATED,
    }
)
"""A plan in one of these occupies its (source, target) pair and its anchor span.

``blocked`` is NOT here on purpose (see :attr:`LinkPlanStatus.BLOCKED`): it would hold the
pair and a slice of the target's diversity budget for ever."""

LIVE_STATUSES: Final = frozenset({_S.DEPLOYED, _S.VERIFIED, _S.MEASURED, _S.LEARNING_CAPTURED})
"""A deployed link. It also occupies the pair while its ``source_page_version`` is >= the
source's current version (the inventory has not yet re-read the page that contains it)."""

ACTOR_PATTERN: Final = (
    r"^(user:\S{1,200}|agent:[a-z0-9_-]{1,64}@[0-9A-Za-z._-]{1,32}|system:[a-z0-9_-]{1,64})$"
)
"""Actor on transitions/events: ``user:<id>``, ``agent:<name>@<version>``, ``system:<name>``."""


def is_allowed_transition(current: LinkPlanStatus, target: LinkPlanStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


type ActorClass = Literal["user", "agent", "system"]

TRANSITION_AUTHORITY: Final[Mapping[LinkPlanStatus, frozenset[ActorClass]]] = {
    _S.PROPOSED: frozenset({"system", "agent"}),
    _S.CRITIQUED: frozenset({"system", "agent"}),
    _S.POLICY_CHECKED: frozenset({"system", "agent"}),
    # ADR-0018 s3: `system:` actors drive the autonomous path, `user:` actors drive
    # overrides. An AGENT is deliberately absent from both: a model may neither decide a
    # publish nor block one (ADR-0012 stands).
    _S.AUTONOMY_DECIDED: frozenset({"system", "user"}),
    _S.BLOCKED: frozenset({"system", "user"}),
    _S.AWAITING_APPROVAL: frozenset({"system", "agent", "user"}),
    # Editorial decisions are HUMAN decisions (AUTONOMY_AND_PERMISSIONS: approval is ASK).
    # Approval is now the EXCEPTION path only (ADR-0018 supersedes ADR-0013 s1).
    _S.APPROVED: frozenset({"user"}),
    _S.REJECTED: frozenset({"user"}),
    _S.SUPERSEDED: frozenset({"user"}),
    # Deployment outcomes are written only by the deployment service (Phase 6), which
    # records the requesting user in its own audit trail.
    _S.VALIDATED: frozenset({"system"}),
    _S.DEPLOYED: frozenset({"system"}),
    _S.VERIFIED: frozenset({"system"}),
    _S.FAILED: frozenset({"system"}),
    _S.ROLLED_BACK: frozenset({"system"}),
    _S.EXPIRED: frozenset({"system"}),
    _S.MEASURED: frozenset({"system"}),
    _S.LEARNING_CAPTURED: frozenset({"system"}),
}
"""Which actor CLASS may move a plan INTO each status (QA review, Phase 3). The class is
the actor prefix; that a ``user:`` actor is authenticated is the API's job (Phase 5)."""


def actor_class(actor: str) -> ActorClass:
    """The class of an actor that already matches ``ACTOR_PATTERN``."""
    prefix = actor.split(":", 1)[0]
    if prefix == "user":
        return "user"
    if prefix == "agent":
        return "agent"
    if prefix == "system":
        return "system"
    raise ValueError("actor has no known class")


def may_transition(actor: str, target: LinkPlanStatus) -> bool:
    return actor_class(actor) in TRANSITION_AUTHORITY[target]


class ScoreComponent(FrozenModel):
    """One weighted component of the transparent score (FR-007)."""

    name: str = Field(description="A ranking-policy weight key (RANKING_COMPONENTS).")
    value: float = Field(ge=0.0, le=1.0, description="Normalised component value.")
    weight: float = Field(ge=0.0)
    contribution: float = Field(ge=0.0, description="value x weight.")

    @model_validator(mode="after")
    def _consistent(self) -> ScoreComponent:
        if self.name not in RANKING_COMPONENTS:
            raise ValueError(f"unknown ranking component {self.name!r}")
        if not math.isclose(self.contribution, self.value * self.weight, abs_tol=1e-9):
            raise ValueError("contribution must equal value x weight")
        return self


class ScoreBreakdown(FrozenModel):
    """``total = relevance x prod(factors)``, where ``relevance = sum(contributions)``.

    Checkable from the record alone: validators enforce both identities.
    """

    components: tuple[ScoreComponent, ...]
    relevance: float = Field(ge=0.0, le=1.0)
    factors: dict[str, float] = Field(
        default_factory=dict, description="Multipliers in (0, 1], e.g. placement_quality."
    )
    total: float = Field(ge=0.0, le=1.0)
    semantic_floor: float | None = Field(
        default=None, description="The cosine floor used for the semantic component (recorded)."
    )
    policy_name: str
    policy_version: int

    @model_validator(mode="after")
    def _identities(self) -> ScoreBreakdown:
        if not math.isclose(
            self.relevance, sum(c.contribution for c in self.components), abs_tol=1e-6
        ):
            raise ValueError("relevance must equal the sum of contributions")
        if any(not 0.0 < factor <= 1.0 for factor in self.factors.values()):
            raise ValueError("factors must be in (0, 1]")
        product = math.prod(self.factors.values()) if self.factors else 1.0
        if not math.isclose(self.total, self.relevance * product, abs_tol=1e-6):
            raise ValueError("total must equal relevance x product(factors)")
        return self


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
    analytics_k: int = Field(
        default=0,
        ge=0,
        le=500,
        description="ADR-0018 / directive s6. DEFAULT 0, so the channel is inert until an "
        "operator enables it: analytics may surface but never alone justify a link.",
    )
    orphan_recovery_k: int = Field(default=0, ge=0, le=500, description="Default 0 (inert).")
    max_candidates_per_source: int = Field(default=80, ge=1, le=2000)
    channels_enabled: tuple[RetrievalChannel, ...] = Field(
        default=(
            RetrievalChannel.SEMANTIC,
            RetrievalChannel.LEXICAL,
            RetrievalChannel.TAXONOMY,
            RetrievalChannel.GRAPH,
        ),
        description="Each channel can be ablated independently (PRD v1.1 section 12.2). The "
        "ANALYTICS and ORPHAN_RECOVERY channels are additive and OFF by default.",
    )


class SelectionPolicy(FrozenModel):
    min_relevance: float = Field(
        default=0.30, ge=0.0, le=1.0, description="Checked BEFORE placement (cheap pruning)."
    )
    min_score: float = Field(
        default=0.35, ge=0.0, le=1.0, description="Checked on the FINAL score, after placement."
    )
    rejection_cooldown_days: int = Field(
        default=90, ge=0, description="A reviewer-rejected pair is not re-proposed for this long."
    )
    max_plans_per_source: int = Field(default=3, ge=1, le=50)
    max_open_plans_per_target: int = Field(
        default=5,
        ge=1,
        le=1000,
        description=(
            "Destination diversity: OPEN plans pointing at one target, from all sources "
            "(existing open plans count, so re-runs are idempotent)."
        ),
    )
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
    min_anchor_words: int = Field(
        default=1, ge=1, le=10, description="For topic-alias anchors (curated phrases)."
    )
    min_anchor_words_non_topic: int = Field(
        default=2, ge=1, le=10, description="For title-core and keyword anchors (less curated)."
    )
    max_anchor_words: int = Field(default=8, ge=1, le=20)
    min_anchor_chars: int = Field(default=4, ge=1, le=50)
    forbidden_anchors: tuple[str, ...] = Field(
        default=("click here", "here", "read more", "learn more", "this page", "this report")
    )
    skip_first_block: bool = Field(
        default=False, description="Avoid placing in the opening paragraph (editorial choice)."
    )
    excluded_ancestors: tuple[str, ...] = Field(
        default=(
            "a", "table", "thead", "tbody", "tr", "td", "th", "blockquote", "figure",
            "figcaption", "pre", "code", "kbd", "samp", "cite", "q", "sup", "sub",
            "h1", "h2", "h3", "h4", "h5", "h6", "caption",
            "label", "legend", "del", "s", "small", "video", "audio", "canvas", "math", "map",
            "header", "footer", "dialog", "address", "details", "summary", "fieldset",
        ),
        description="The anchor span may not sit under any of these (PRD v1.1 section 12.4).",
    )  # fmt: skip


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
    excluded_target_paths: tuple[str, ...] = Field(
        default=("^/$",),
        description=(
            "URL-path regexes that are never link TARGETS (the home page is a listing, and a "
            "taxonomy match on it is an artefact). Matched against the canonical URL path."
        ),
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

    @field_validator("excluded_target_paths")
    @classmethod
    def _paths_compile(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in value:
            re.compile(pattern)
        return value

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

    # Locating rule for Phase 6: find the block by (context_hash, anchor text,
    # occurrence_index) in the CURRENT body; block_id is only a hint (ordinal ids
    # shift when blocks are inserted above). Offsets count characters of the
    # normalised (NFC, whitespace-collapsed) block text, never HTML.


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
    block_id: str | None = Field(
        default=None,
        description="The current block the plan maps to (fresh/rebasable); None when stale.",
    )
    reasons: tuple[str, ...] = Field(
        default=(), description="version_changed | block_missing | context_changed | anchor_moved"
    )


@runtime_checkable
class ExperimentGuard(Protocol):
    """Hook for PRD 7.5 'an active experiment forbids modification' (filled in Phase 7).

    Phase 3's implementation locks nothing; the filter chain already consults it, so
    adding experiments changes no recommendation code path.
    """

    async def locked(self, *, source_page_id: UUID, target_page_id: UUID) -> bool: ...


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
    anchor_source: Literal["target_topic", "target_title", "target_keyword", "edited"] = Field(
        description="'edited' (Phase 5) must still be copied verbatim from the block text."
    )
    locator: PlacementLocator
    preview: PlacementPreview
    score: ScoreBreakdown
    confidence: float = Field(ge=0.0, le=1.0, description="Raw in Phase 3; calibrated later.")
    calibration_version: str | None = Field(
        default=None,
        description="None = RAW confidence. No threshold may treat raw as calibrated.",
    )
    reason_codes: tuple[ReasonCode, ...]
    rationale: str = Field(description="Deterministic sentence(s) rendered from reason codes.")
    engine_version: str
    policy_version: int = Field(ge=1, description="Recommendation policy version.")
    ranking_policy_version: int = Field(ge=1)
    embedding_model: str | None = Field(default=None, description="Provider-reported model.")
    reasoning_model: str | None = Field(
        default=None, description="Phase 4: the model (by role) that critiqued or planned, if any."
    )
    analysis_version: str
    supersedes: UUID | None = None
    status: LinkPlanStatus
    created_at: AwareDatetime
    updated_at: AwareDatetime


class GraphSignals(FrozenModel):
    """The internal link-graph position of ONE page version (``page_graph_metrics``).

    Every signal is reported SEPARATELY and deliberately: there is no single opaque "graph
    score". A reviewer, an eval and the learning pipeline must all be able to see WHICH
    property moved a candidate, and a composite number destroys exactly that. The ranking
    policy is free to combine them into its ``graph`` component; the record keeps the parts.
    """

    page_id: UUID
    page_version: int = Field(ge=1)
    in_degree: int = Field(ge=0, description="Current internal inbound links.")
    out_degree: int = Field(ge=0, description="Current internal outbound links.")
    internal_pagerank: float = Field(ge=0.0, le=1.0, description="Normalised; sums to ~1.")
    hub_score: float = Field(ge=0.0, le=1.0)
    authority_score: float = Field(ge=0.0, le=1.0)
    crawl_depth: int | None = Field(
        default=None, ge=0, description="Clicks from the configured entry page; None = unreachable."
    )
    is_orphan: bool = Field(description="No internal inbound link from any current page.")
    is_nearly_orphan: bool = Field(description="Inbound links below the policy threshold.")
    is_bridge: bool = Field(
        description="Removing this page would disconnect part of the graph (a cut vertex)."
    )
    component_id: int | None = Field(
        default=None, ge=0, description="Weakly connected component ordinal."
    )
    cluster_id: int | None = Field(default=None, ge=0, description="Topical cluster ordinal.")
    metrics_version: str = Field(
        min_length=1,
        max_length=64,
        description="Algorithm/recipe version, e.g. 'graph-alg1'. Stored with every row so a "
        "recomputation is attributable and two versions are never compared.",
    )
    computed_at: AwareDatetime


class CandidateAttribution(FrozenModel):
    """Why ONE channel offered this candidate -- the per-channel audit trail (directive s6).

    One row per channel that retrieved the candidate. ``raw`` is the channel's own units
    (cosine, ts_rank_cd, edge weight, an analytics rate); ``normalised`` is the comparable
    [0, 1] value the union used. Both are kept because the raw value is the only one that can
    be re-derived from the stores, and the normalised one is the only one that explains the
    ranking.
    """

    channel: RetrievalChannel
    raw: float
    normalised: float = Field(ge=0.0, le=1.0)
    rank_in_channel: int = Field(ge=1)
    evidence: dict[str, Any] = Field(
        default_factory=dict,
        description="Channel-specific facts (the matched relationship type, the keyword hits, "
        "the vector's point id and content hash). Never free model text.",
    )


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
    attribution: tuple[CandidateAttribution, ...] = Field(
        default=(),
        description="Per-channel attribution, one entry per channel in ``channels`` "
        "(additive; empty for rows written before ADR-0018).",
    )
    graph_signals: GraphSignals | None = Field(
        default=None, description="The TARGET's link-graph position when it was retrieved."
    )

    @model_validator(mode="after")
    def _attribution_matches_channels(self) -> CandidateView:
        if not self.attribution:
            return self
        attributed = [entry.channel for entry in self.attribution]
        if len(set(attributed)) != len(attributed):
            raise ValueError("at most one attribution entry per channel")
        if set(attributed) != set(self.channels):
            raise ValueError("attribution must cover exactly the retrieving channels")
        return self


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
    target_graph_signals: GraphSignals | None = Field(
        default=None,
        description="The destination's link-graph position, read at request time (additive; "
        "None when graph metrics have not been computed). Deliberately not a plan COLUMN: "
        "plan content is immutable, and the graph moves under it.",
    )


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


class ModuleKind(StrEnum):
    """What relation a related-reports module expresses. Additive (ADR-0027 §1b/§1c).

    One member today, deliberately. ``adjacent`` -- "a reader of an EV report wants charging
    stations and batteries" -- needs the typed market graph the taxonomy does not declare
    (NEXT_STEPS items 27 and 32), so it is absent rather than guessed at.
    """

    REGIONAL_SIBLING = "regional_sibling"
    """The same market in another geography: the owner's Priority 1."""


class PageModuleItemView(FrozenModel):
    """One entry of a module, in the order it would render."""

    rank: int = Field(ge=1)
    target_page_id: UUID
    target_url: str
    target_title: str | None = None
    geography_label: str | None = None
    reason_codes: list[ReasonCode]


class PageModuleView(FrozenModel):
    """A whole module as proposed for one page -- the unit an editor judges.

    A module is a PROPOSAL and nothing more. Unlike a ``LinkPlan`` it has no status, because
    approving one would mean composing it onto a page, and how that happens is still open
    (NEXT_STEPS item 12, the CMS surface). It is replaced wholesale by each run, like
    ``opportunities``: there is no partial edit to preserve.
    """

    page_id: UUID
    canonical_url: str
    title: str | None = None
    kind: ModuleKind
    market_core: str
    """The market identity every item shares with the page -- the module's coherence gate."""
    geography_label: str | None = None
    """The page's own geography; ``None`` for a global page."""
    items: list[PageModuleItemView]
    run_id: UUID
    analysis_version: str
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
    supersedes: UUID | None = Field(
        default=None,
        description="Phase 5: the plan an edited plan replaces (additive; event_version 1).",
    )


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

    async def list_modules(
        self,
        *,
        limit: int,
        offset: int,
        kind: ModuleKind | None = None,
        page_id: UUID | None = None,
    ) -> ListResponse[PageModuleView]: ...

    async def get_summary(self) -> RecommendationSummary: ...
