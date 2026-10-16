"""Learning: proposals, skills, eval datasets, evaluation runs, experiments, the gate policy.

FROZEN CONTRACT (orchestrator). Phase 8, docs/adr/ADR-0016-learning.md -- whose **section 15
amendments are binding and override sections 1-14**, with section 16 recording the
orchestrator's decisions. Nothing here is additive to an earlier phase: this module is new.

The whole phase exists to make CLAUDE.md's "Learning Design Rule" a MECHANISM rather than a
promise::

    experience -> structured feedback -> proposal -> offline eval -> shadow -> canary
               -> experiment -> explicit promotion (human, ASK-level)

Five rules are load-bearing, and each is enforced by code rather than by review:

1. **Agents propose; only a human promotes.** A proposal's ``proposed_by`` matches
   ``^(system|agent):`` and the transition into ``promoted`` must carry a ``^user:`` actor and
   an ``evaluation_run_id`` -- both as database CHECKs (migration 0009), on top of a
   same-transaction ``txid`` guard and the transaction-local ``ken.learning_promoter`` setting
   (ADR-0016 section 15.7.1/15.7.2). :data:`PROPOSAL_TRANSITION_AUTHORITY` is the contract-level
   copy of the same rule.
2. **Some fields can never be learned.** :data:`FROZEN_POLICY_PATHS` and
   :data:`ALLOWED_CHANGE_PATHS` live HERE, as ``Final`` frozensets of dotted paths, and
   :class:`PolicyProposalBody` refuses a change outside the allowlist or inside the frozen set.
   ``policies/learning/learning.v1.yaml`` may only NARROW both (ADR-0016 section 15.7.5) -- a
   human-editable document can take a field away from the allowlist but can never add one.
3. **A model chooses; it never writes a number.** Reflection enumerates the options
   deterministically and the model returns a :class:`ProposalDraft` whose ``choice_index`` is
   validated against the live option count (ADR-0016 section 15.7.7). The full option set is
   persisted in :class:`ProposalEvidence`, so the choice is reconstructable.
4. **Behavioural signals may only BLOCK a gate, never pass one** (ADR-0016 section 2 / 16.7).
   First-party counts are spoofable (ADR-0015 section 12.3) and search data is confounded; only
   the editorial stream may pass a gate. The wrapper that enforces it is
   ``ken_services.learning.protocol.BlockOnly``.
5. **Phase 8 promotes nothing, by construction.** ``Settings.learning_demonstration_mode``
   defaults ``True`` and the promotion service refuses ``promoted`` with
   :attr:`PromotionRefusal.DEMONSTRATION_MODE` while it is set; a decision class below
   :data:`MIN_SAMPLE_COUNT` makes a gate :attr:`GateResult.INCONCLUSIVE`, never
   :attr:`GateResult.PASS` (ADR-0016 section 15.4 / 16.1). The corpus is ~65 synthetic pages:
   every number this phase produces is a demonstration, not evidence.

**Stages.** :class:`ProposalStatus` carries the FULL ladder CLAUDE.md line 88, PRD.md section
10 and ``evals/README.md`` sections 2-3 mandate -- ``proposed -> offline_test -> shadow ->
canary -> experiment -> promoted``, plus ``rejected``, ``rolled_back`` and ``withdrawn``. The
gate policy declares the MINIMUM required path per kind (ADR-0016 section 15.3), so the ladder
may be traversed in part; the stages themselves are never removed from the contract. In Phase 8
only ``offline_test`` and ``shadow`` have working machinery: ``canary`` and ``experiment`` are
**stage defined, machinery deferred -- see ADR-0016 section 16** (16.3 defers canary, 16.6 ships
``policy_ab`` as plumbing only).

Event vocabulary: unchanged. ``skill.proposed``, ``skill.promoted``, ``policy.proposed`` and
``policy.promoted`` already exist (``skill.promoted`` is a governed addition recorded in
docs/CONTRACTS.md section 12, NOT a TRD section 15 name). Stage changes live in the append-only
``learning_proposal_transitions`` table, and a finished evaluation is announced by
``job.completed`` with ``kind=evaluation``. The two new typed payloads are
``PolicyProposedPayload`` / ``SkillProposedPayload`` in :mod:`ken_contracts.events`, which also
holds the payload registry; this module deliberately does NOT import that one, so the
dependency runs events -> learning only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date
from enum import StrEnum
from typing import Annotated, Any, Final, Literal, Protocol, Self, runtime_checkable
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    Field,
    field_validator,
    model_validator,
)

from ken_contracts.agents import FallbackReason, ModelText
from ken_contracts.analytics import AnalyticsSource, ConfounderFlag, MetricTotals
from ken_contracts.common import FrozenModel, OperationId, reject_unstorable_text
from ken_contracts.errors import ListResponse
from ken_contracts.intelligence import RelationshipType
from ken_contracts.model import ModelRole
from ken_contracts.recommendations import (
    RANKING_COMPONENTS,
    ActorClass,
    RetrievalChannel,
    actor_class,
)

# --------------------------------------------------------------------------- #
# Constants (frozen; migration 0009 keeps literal copies, parity-tested)
# --------------------------------------------------------------------------- #
LEARNING_ACTOR: Final = "system:learning"
"""The only actor the reflection and evaluation jobs use. It never reaches ``promoted``."""
LEARNING_EMITTED_BY: Final = "ken-learning"
SUBJECT_LEARNING_PROPOSAL: Final = "learning_proposal"
"""``subject_type`` of ``policy.proposed`` / ``skill.proposed``; ``subject_id`` is the id."""
LEARNING_PROMOTER_SETTING: Final = "ken.learning_promoter"
"""Transaction-local GUC (the ``ken.analytics_writer`` pattern, migration 0008 line 50):
``SELECT set_config('ken.learning_promoter', 'on', true)`` lets the SAME transaction record a
transition into ``promoted``. Exactly one code path (``ken_services.learning.promote``) sets
it, so a CLI one-off, a data fix or a test helper cannot forge a promotion."""

MIN_SAMPLE_COUNT: Final = 30
"""Floor per decision class (ADR-0016 section 15.4 / 16.1). Below it a gate is
``inconclusive``, never ``pass``. The policy may raise this number and never lower it."""
MAX_HOLDOUT_TREATMENT_FRACTION: Final = 0.20
"""Hard maximum share of pages a ``holdout`` may withhold links from. CHECKed in the database
too: a human-editable policy cannot raise it."""
DEFAULT_HOLDOUT_TREATMENT_FRACTION: Final = 0.10
"""The policy default, half the hard maximum."""
EXPERIMENT_ACTIVATOR_SETTING: Final = "ken.learning_experiment_activator"
"""Transaction-local GUC (the :data:`LEARNING_PROMOTER_SETTING` pattern) required to move a
``holdout`` from ``draft`` to ``active``. ``ken_services.learning.experiments`` sets it, and
only after checking ``Settings.experiments_allow_holdout`` -- so "the user has not enabled
holdouts" is enforced by the database and not only by a Python branch."""
MAX_TITLE_CHARS: Final = 200
MAX_RATIONALE_CHARS: Final = 2000
MAX_NOTE_CHARS: Final = 500
MAX_DESCRIPTION_CHARS: Final = 500
MAX_INSTRUCTIONS_CHARS: Final = 8000
MAX_CHANGES_PER_PROPOSAL: Final = 50
MAX_OPTIONS_PER_PROPOSAL: Final = 10
MAX_OBSERVATIONS: Final = 500
MAX_CASE_ID_CHARS: Final = 120
MAX_SOURCE_REFS: Final = 20

SKILL_NAME_PATTERN: Final = r"^[a-z][a-z0-9_]{2,63}$"
EXPERIMENT_KEY_PATTERN: Final = r"^[a-z0-9][a-z0-9_-]{2,63}$"
CASE_ID_PATTERN: Final = r"^[a-z0-9][a-z0-9-]{2,119}$"
SHA256_PATTERN: Final = r"^[0-9a-f]{64}$"
VERSION_PATTERN: Final = r"^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$"
"""A policy or skill version STAMP as it appears in a plan or a file name (e.g. ``3``,
``v2``, ``2026-10-16``). Opaque: never parsed for ordering."""
PATH_PATTERN: Final = r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$"
"""A dotted policy path, e.g. ``recommendation.selection.min_score``."""
PROPOSER_PATTERN: Final = (
    r"^(system:[a-z0-9_-]{1,64}" r"|agent:[a-z0-9_-]{1,64}@[0-9A-Za-z._-]{1,32})$"
)
"""``learning_proposals.proposed_by``: the ``system`` and ``agent`` branches of
``ACTOR_PATTERN`` and nothing else. A proposal can never claim a human author -- migration
0009 keeps the same CHECK, so the rule survives a code path that forgets it."""
PROMOTER_ACTOR_PATTERN: Final = r"^user:[a-z0-9][a-z0-9._-]{2,63}$"
"""``^user:`` + ``ken_contracts.auth.USERNAME_PATTERN``: the actor shape every transition into
``promoted`` / ``rejected`` / ``rolled_back`` must have (a DB CHECK). The database can only
check the SHAPE; authenticity comes from the session, which is why the promotion service takes
a ``Principal`` (ADR-0016 15.2b)."""


def _storable(value: str) -> str:
    return reject_unstorable_text(value)


type TitleText = Annotated[
    str, Field(min_length=1, max_length=MAX_TITLE_CHARS), AfterValidator(_storable)
]
type RationaleText = Annotated[
    str, Field(min_length=1, max_length=MAX_RATIONALE_CHARS), AfterValidator(_storable)
]
type NoteText = Annotated[str, Field(max_length=MAX_NOTE_CHARS), AfterValidator(_storable)]
type DescriptionText = Annotated[
    str, Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS), AfterValidator(_storable)
]
type PolicyPath = Annotated[str, Field(min_length=1, max_length=200, pattern=PATH_PATTERN)]
type AnchorSource = Literal["target_topic", "target_title", "target_keyword", "edited"]
"""Copy of ``LinkPlan.anchor_source``: one third of a decision class."""


# --------------------------------------------------------------------------- #
# Proposals: kinds, the full stage ladder, transition authority
# --------------------------------------------------------------------------- #
class ProposalKind(StrEnum):
    POLICY = "policy"
    """A change to an allowlisted policy path; the candidate document is stored whole."""
    SKILL = "skill"
    """A new skill version. Phase 8's only consumer is the eval runner (ADR-0016 16.5)."""


class TargetKind(StrEnum):
    """Which policy document a ``policy`` proposal targets. ``None`` for a skill proposal."""

    RANKING = "ranking"
    RECOMMENDATION = "recommendation"
    AGENTS = "agents"


class ProposalStatus(StrEnum):
    """The lifecycle PRD.md section 10 and CLAUDE.md's Learning Design Rule mandate.

    Every stage is part of the CONTRACT even where Phase 8 ships no machinery for it: removing
    a stage would bake a deviation from the governing specs into the schema. What Phase 8
    implements is a scheduling choice, recorded per stage below.
    """

    PROPOSED = "proposed"
    """Inserted by the reflection job. Zero production effect."""
    OFFLINE_TEST = "offline_test"
    """``run_evaluation(mode=offline)`` replayed the eval corpus against the candidate."""
    SHADOW = "shadow"
    """The candidate re-ran beside production, writing no ``link_plans`` (ranking /
    recommendation only). Implemented in Phase 8."""
    CANARY = "canary"
    """Stage defined, machinery deferred -- see ADR-0016 section 16 (16.3): 48 plans yield
    almost no information, so no code path uses a candidate policy for real in Phase 8."""
    EXPERIMENT = "experiment"
    """Stage defined, machinery deferred -- see ADR-0016 section 16 (16.6): ``policy_ab``
    ships as plumbing (tables, deterministic assignment, the real guard) with ``draft`` status
    and no activation path."""
    PROMOTED = "promoted"
    """Human, ASK-level, with a passing ``policy_evaluations`` run. Refused outright while
    ``Settings.learning_demonstration_mode`` is set, which is the Phase 8 default."""
    REJECTED = "rejected"
    """Terminal; human; carries a structured reason."""
    ROLLED_BACK = "rolled_back"
    """A promoted version was retired and its predecessor restored. The old file is never
    edited or deleted."""
    WITHDRAWN = "withdrawn"
    """Terminal; human. Only a human withdraws a proposal (ADR-0016 15.3 cut automatic
    superseding: auto-withdrawing a human-visible proposal is a silent state change)."""


_P: Final = ProposalStatus

PROPOSAL_TRANSITIONS: Final[Mapping[ProposalStatus, frozenset[ProposalStatus]]] = {
    _P.PROPOSED: frozenset({_P.OFFLINE_TEST, _P.REJECTED, _P.WITHDRAWN}),
    _P.OFFLINE_TEST: frozenset(
        {_P.SHADOW, _P.CANARY, _P.EXPERIMENT, _P.PROMOTED, _P.REJECTED, _P.WITHDRAWN}
    ),
    _P.SHADOW: frozenset({_P.CANARY, _P.EXPERIMENT, _P.PROMOTED, _P.REJECTED, _P.WITHDRAWN}),
    _P.CANARY: frozenset({_P.EXPERIMENT, _P.PROMOTED, _P.REJECTED, _P.WITHDRAWN}),
    _P.EXPERIMENT: frozenset({_P.PROMOTED, _P.REJECTED, _P.WITHDRAWN}),
    _P.PROMOTED: frozenset({_P.ROLLED_BACK}),
    _P.REJECTED: frozenset(),
    _P.ROLLED_BACK: frozenset(),
    _P.WITHDRAWN: frozenset(),
}
"""Allowed stage changes. ``proposed`` can NEVER reach ``promoted`` directly: at least one
``offline_test`` run stands between a hypothesis and production. Stages may be skipped
forward, because the gate policy -- not this table -- declares the minimum required path per
kind (:attr:`GatePolicy.required_stages`, ADR-0016 15.3)."""

OPEN_PROPOSAL_STATUSES: Final = frozenset(
    {_P.PROPOSED, _P.OFFLINE_TEST, _P.SHADOW, _P.CANARY, _P.EXPERIMENT}
)
"""Statuses in which a proposal is still live. The partial UNIQUE index on ``fingerprint``
covers exactly these, which is what makes a reflection re-run idempotent."""
TERMINAL_PROPOSAL_STATUSES: Final = frozenset({_P.REJECTED, _P.ROLLED_BACK, _P.WITHDRAWN})
HUMAN_ONLY_STATUSES: Final = frozenset(
    {_P.CANARY, _P.EXPERIMENT, _P.PROMOTED, _P.REJECTED, _P.ROLLED_BACK, _P.WITHDRAWN}
)
"""Statuses only an authenticated human may reach. Migration 0009 enforces the subset
``{promoted, rejected, rolled_back}`` as a CHECK (the database can only test an actor's
SHAPE); the rest is the promotion service's job, which is why it takes a ``Principal``."""

PROPOSAL_TRANSITION_AUTHORITY: Final[Mapping[ProposalStatus, frozenset[ActorClass]]] = {
    # The insert status: the reflection job (system) or, in a later phase, an agent proposal.
    _P.PROPOSED: frozenset({"system", "agent"}),
    _P.OFFLINE_TEST: frozenset({"system", "user"}),
    _P.SHADOW: frozenset({"system", "user"}),
    _P.CANARY: frozenset({"user"}),
    _P.EXPERIMENT: frozenset({"user"}),
    _P.PROMOTED: frozenset({"user"}),
    _P.REJECTED: frozenset({"user"}),
    _P.ROLLED_BACK: frozenset({"user"}),
    _P.WITHDRAWN: frozenset({"user"}),
}
"""Which actor CLASS may move a proposal INTO each status. No ``agent`` appears anywhere
except the insert status: an agent may propose and can never advance its own proposal
(ADR-0012: agents are REVIEW-only)."""


def is_allowed_proposal_transition(current: ProposalStatus, target: ProposalStatus) -> bool:
    return target in PROPOSAL_TRANSITIONS[current]


def may_advance_proposal(actor: str, target: ProposalStatus) -> bool:
    """Whether ``actor``'s CLASS may move a proposal into ``target``.

    That a ``user:`` actor is AUTHENTICATED is not knowable here and not knowable in the
    database either: it is the API's job, which is why ``PromotionService.advance`` takes a
    ``Principal`` and derives the actor from the session (ADR-0016 15.2b / 15.7.3).
    """
    return actor_class(actor) in PROPOSAL_TRANSITION_AUTHORITY[target]


class ProposalRejectionReason(StrEnum):
    """Structured reason required with ``rejected`` / ``rolled_back`` (ADR-0013's rule that an
    editorial decision carries a code, not only prose)."""

    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    """Below the sample floor, or the evidence does not re-derive."""
    GATE_FAILED = "gate_failed"
    FROZEN_FIELD = "frozen_field"
    OUT_OF_SCOPE = "out_of_scope"
    RISK_TOO_HIGH = "risk_too_high"
    SUPERSEDED_BY_NEWER = "superseded_by_newer"
    DEMONSTRATION_ONLY = "demonstration_only"
    """The corpus is synthetic: the proposal is sound but may not be promoted (16.1)."""
    REGRESSION_OBSERVED = "regression_observed"
    """``rolled_back`` only."""
    OPERATIONAL_INCIDENT = "operational_incident"
    """``rolled_back`` only."""


REASONS_BY_PROPOSAL_STATUS: Final[Mapping[ProposalStatus, frozenset[ProposalRejectionReason]]] = {
    _P.REJECTED: frozenset(
        {
            ProposalRejectionReason.INSUFFICIENT_EVIDENCE,
            ProposalRejectionReason.GATE_FAILED,
            ProposalRejectionReason.FROZEN_FIELD,
            ProposalRejectionReason.OUT_OF_SCOPE,
            ProposalRejectionReason.RISK_TOO_HIGH,
            ProposalRejectionReason.SUPERSEDED_BY_NEWER,
            ProposalRejectionReason.DEMONSTRATION_ONLY,
        }
    ),
    _P.ROLLED_BACK: frozenset(
        {
            ProposalRejectionReason.REGRESSION_OBSERVED,
            ProposalRejectionReason.OPERATIONAL_INCIDENT,
            ProposalRejectionReason.RISK_TOO_HIGH,
        }
    ),
    _P.WITHDRAWN: frozenset(
        {
            ProposalRejectionReason.SUPERSEDED_BY_NEWER,
            ProposalRejectionReason.OUT_OF_SCOPE,
            ProposalRejectionReason.DEMONSTRATION_ONLY,
        }
    ),
}
"""A reason is REQUIRED for exactly these target statuses, and must come from the set."""


# --------------------------------------------------------------------------- #
# The two path sets. THIS is the contract boundary (ADR-0016 15.2c / 15.7.5)
# --------------------------------------------------------------------------- #
FROZEN_POLICY_PATHS: Final[frozenset[str]] = frozenset(
    {
        # Placement safety: which blocks, which ancestors, which anchors, anchor bounds.
        "recommendation.placement",
        # Link density and the cooldown that protects a human's "no".
        "recommendation.selection.max_internal_links_per_100_words",
        "recommendation.selection.max_links_per_block",
        "recommendation.selection.rejection_cooldown_days",
        "recommendation.excluded_target_paths",
        # Protected pages and the whole taxonomy.
        "taxonomy",
        # Measurement must never be tuned by the thing it measures.
        "analytics",
        # Process configuration, model identity, autonomy. None of it is policy DATA.
        "settings",
        "model",
        "auto_approve",
        "autonomy",
        # The learning policy itself: the frozen list may not be learned either (15.2c).
        "learning",
    }
)
"""Dotted paths that may NEVER be changed by a proposal, in Phase 8 or any later one. A frozen
entry covers itself and everything beneath it (:func:`is_frozen_path`).

ADR-0016 section 2 allows three of these DIRECTIONALLY (density caps downward, the cooldown
upward, excluded paths by growing). Section 5's allowlist does not list them, so Phase 8
freezes them outright: a directional rule is a mechanism that does not exist yet, and the
conservative reading of section 15 is to refuse rather than to approximate it. A later phase may
add a typed directional rule; until then there is no path by which a model-influenced proposal
can loosen a safety bound."""

ALLOWED_CHANGE_PATHS: Final[frozenset[str]] = frozenset(
    {
        "recommendation.selection.min_relevance",
        "recommendation.selection.min_score",
        "recommendation.selection.max_plans_per_source",
        "recommendation.selection.max_open_plans_per_target",
        "recommendation.retrieval.semantic_k",
        "recommendation.retrieval.lexical_k",
        "recommendation.retrieval.graph_k",
        "recommendation.retrieval.max_candidates_per_source",
        "recommendation.retrieval.channels_enabled",
        "recommendation.intent_compatibility",
        "recommendation.page_type_targets",
        "recommendation.geography_scores",
        "recommendation.underlinked_inbound_threshold",
        "agents.critic.accept_min_confidence",
        "agents.max_plans_per_run",
    }
    | {f"ranking.weights.{component}" for component in RANKING_COMPONENTS}
)
"""The ONLY paths a proposal may change (ADR-0016 section 5). The YAML gate policy may drop
entries from this set and can never add one: :class:`ChangePolicy` validates that, and a test
asserts a widened YAML is refused."""

CROSS_CHECKED_PATHS: Final[frozenset[str]] = frozenset(
    {"recommendation.selection.max_plans_per_source", "agents.max_plans_per_run"}
)
"""Paths that participate in the ADR-0012 section 10 load-time cross-check
(``agents.max_plans_per_run <= recommendation.selection.max_plans_per_source``). A proposal
touching one of these must declare :attr:`PolicyProposalBody.cross_check`, so a candidate that
would make the agents policy fail to load cannot be constructed at all."""


def is_frozen_path(path: str) -> bool:
    """Whether ``path`` lies in, or beneath, :data:`FROZEN_POLICY_PATHS`."""
    return any(path == frozen or path.startswith(f"{frozen}.") for frozen in FROZEN_POLICY_PATHS)


def is_learnable_path(path: str) -> bool:
    """Whether a proposal may change ``path``. Frozen always wins over the allowlist."""
    return path in ALLOWED_CHANGE_PATHS and not is_frozen_path(path)


class PolicyChange(FrozenModel):
    """One parameter change: the path, the value it had, the value proposed.

    ``from_value`` is what the BASE document held, so the diff a human sees on the promotion
    screen is reconstructable from the row alone. Values are JSON scalars, arrays or objects;
    they are never rendered as code and never interpolated into SQL.
    """

    path: PolicyPath
    from_value: Any = Field(description="The value in the base document (JSON).")
    to_value: Any = Field(description="The proposed value (JSON).")

    @field_validator("path")
    @classmethod
    def _learnable(cls, value: str) -> str:
        if is_frozen_path(value):
            raise ValueError(f"{value!r} is a frozen field and can never be learned")
        if value not in ALLOWED_CHANGE_PATHS:
            raise ValueError(f"{value!r} is not in the learning change allowlist")
        return value

    @model_validator(mode="after")
    def _actually_changes(self) -> Self:
        if self.from_value == self.to_value:
            raise ValueError("a change must change something")
        return self


class CrossPolicyContext(FrozenModel):
    """The companion values a candidate must respect (ADR-0012 section 10 cross-check).

    ``agents.v1.yaml``'s ``max_plans_per_run`` must stay ``<=`` the recommendation policy's
    ``selection.max_plans_per_source``; the agents loader enforces it and RAISES, so a
    proposal that lowers the cap below the live ``max_plans_per_run`` would make the agents
    policy unloadable. Reflection records both live values here and
    :class:`PolicyProposalBody` refuses the combination -- the alternative (dropping
    ``max_plans_per_source`` from the allowlist) would lose a genuinely useful knob.
    """

    agents_max_plans_per_run: int = Field(ge=1, le=50, description="Live agents policy value.")
    recommendation_max_plans_per_source: int = Field(
        ge=1, le=50, description="Live recommendation policy value."
    )

    @model_validator(mode="after")
    def _base_is_consistent(self) -> Self:
        if self.agents_max_plans_per_run > self.recommendation_max_plans_per_source:
            raise ValueError(
                "the live policies already violate the ADR-0012 cross-check; refusing to "
                "build a proposal on an inconsistent base"
            )
        return self


class PolicyProposalBody(FrozenModel):
    """``learning_proposals.body`` for a ``policy`` proposal. THE frozen-field boundary."""

    kind: Literal[ProposalKind.POLICY] = ProposalKind.POLICY
    target_kind: TargetKind
    base_version: str = Field(pattern=VERSION_PATTERN)
    changes: tuple[PolicyChange, ...] = Field(min_length=1, max_length=MAX_CHANGES_PER_PROPOSAL)
    cross_check: CrossPolicyContext | None = Field(
        default=None,
        description="Required when a change touches CROSS_CHECKED_PATHS; else optional.",
    )

    @model_validator(mode="after")
    def _paths_are_unique_and_in_scope(self) -> Self:
        paths = [change.path for change in self.changes]
        if len(set(paths)) != len(paths):
            raise ValueError("a proposal changes each path at most once")
        prefix = self.target_kind.value
        for path in paths:
            if path != prefix and not path.startswith(f"{prefix}."):
                raise ValueError(f"{path!r} does not belong to the {prefix} policy")
        return self

    @model_validator(mode="after")
    def _cross_check_holds(self) -> Self:
        touched = {change.path for change in self.changes} & CROSS_CHECKED_PATHS
        if not touched:
            return self
        if self.cross_check is None:
            raise ValueError(
                "a change to "
                + ", ".join(sorted(touched))
                + " must declare cross_check (ADR-0012 section 10)"
            )
        by_path = {change.path: change.to_value for change in self.changes}
        cap = by_path.get(
            "recommendation.selection.max_plans_per_source",
            self.cross_check.recommendation_max_plans_per_source,
        )
        per_run = by_path.get("agents.max_plans_per_run", self.cross_check.agents_max_plans_per_run)
        if not isinstance(cap, int) or not isinstance(per_run, int):
            raise ValueError("max_plans_per_source and max_plans_per_run are integers")
        if per_run > cap:
            raise ValueError(
                "the candidate would make the agents policy unloadable: "
                f"max_plans_per_run ({per_run}) exceeds max_plans_per_source ({cap})"
            )
        return self

    @property
    def change_paths(self) -> tuple[str, ...]:
        return tuple(sorted(change.path for change in self.changes))


class SkillSource(StrEnum):
    HUMAN = "human"
    REFLECTION = "reflection"


def _json_schema_object(value: dict[str, Any]) -> dict[str, Any]:
    """A skill's input/output schema must be a JSON-Schema OBJECT, not an arbitrary blob."""
    if value.get("type") != "object":
        raise ValueError("a skill schema must be a JSON Schema of type 'object'")
    if not isinstance(value.get("properties", {}), dict):
        raise ValueError("a skill schema's 'properties' must be an object")
    return value


class SkillProposalBody(FrozenModel):
    """``learning_proposals.body`` for a ``skill`` proposal.

    Content is immutable once stored: a revision is a new ``version`` row, never an edit
    (``ken_skill_guard``). Phase 8's only consumer is the eval runner, which executes the
    instructions through ``ModelRole.REASONING`` against the suites the skill declares --
    which is what turns TRD section 17's ``evaluation_score`` into a measured number rather
    than a stored opinion. Wiring a promoted skill into the agent graph is Phase 9 and needs
    its own prompt-injection analysis (ADR-0016 16.5).
    """

    kind: Literal[ProposalKind.SKILL] = ProposalKind.SKILL
    name: str = Field(pattern=SKILL_NAME_PATTERN)
    version: int = Field(ge=1)
    description: DescriptionText
    instructions: str = Field(min_length=1, max_length=MAX_INSTRUCTIONS_CHARS)
    input_schema: Annotated[dict[str, Any], AfterValidator(_json_schema_object)]
    output_schema: Annotated[dict[str, Any], AfterValidator(_json_schema_object)]
    source: SkillSource
    suites: tuple[EvalSuite, ...] = Field(
        min_length=1, description="The suites this skill is evaluated against."
    )
    model_role: ModelRole = Field(
        default=ModelRole.REASONING, description="The role the runner executes it under."
    )

    @field_validator("instructions")
    @classmethod
    def _storable_instructions(cls, value: str) -> str:
        return reject_unstorable_text(value)


type ProposalBody = PolicyProposalBody | SkillProposalBody


# --------------------------------------------------------------------------- #
# Decision class: a TYPED contract with a canonical string form (15.6 / 15.7.11)
# --------------------------------------------------------------------------- #
NO_RELATIONSHIP: Final = "none"
"""Sentinel for a decision class with no relationship type. Not a RelationshipType member
(asserted by a test), so the canonical form stays unambiguous."""
DECISION_CLASS_PATTERN: Final = r"^[a-z][a-z0-9_]*\|[a-z][a-z0-9_]*\|[a-z]+(\+[a-z]+)*$"
"""The canonical form ``<relationship|none>|<anchor_source>|<channel>[+<channel>...]``.
``metrics.by_decision_class`` is keyed by exactly this, so Phase 9 reads per-class numbers
without a schema change."""

_CANONICAL = re.compile(DECISION_CLASS_PATTERN)


class DecisionClass(FrozenModel):
    """Relationship type x anchor source x channel set (TRD section 20, ADR-0016 15.6).

    Phase 9 graduates autonomy PER CLASS, so the class must be a contract rather than a label
    invented twice. ``channels`` is normalised (sorted, unique) on construction, which makes
    :attr:`canonical` a stable key.
    """

    relationship_type: RelationshipType | None = None
    anchor_source: AnchorSource
    channels: tuple[RetrievalChannel, ...] = Field(min_length=1)

    @field_validator("channels")
    @classmethod
    def _normalised(cls, value: tuple[RetrievalChannel, ...]) -> tuple[RetrievalChannel, ...]:
        if len(set(value)) != len(value):
            raise ValueError("channels must be unique")
        return tuple(sorted(value, key=lambda channel: channel.value))

    @property
    def canonical(self) -> str:
        """The stable string key. Deterministic and round-trips through :meth:`parse`."""
        relationship = self.relationship_type.value if self.relationship_type else NO_RELATIONSHIP
        channels = "+".join(channel.value for channel in self.channels)
        return f"{relationship}|{self.anchor_source}|{channels}"

    @classmethod
    def parse(cls, value: str) -> DecisionClass:
        """Inverse of :attr:`canonical`. Raises ``ValueError`` on anything else."""
        if not _CANONICAL.fullmatch(value):
            raise ValueError("not a canonical decision class")
        relationship_part, anchor_part, channel_part = value.split("|")
        return cls(
            relationship_type=(
                None
                if relationship_part == NO_RELATIONSHIP
                else RelationshipType(relationship_part)
            ),
            anchor_source=_anchor_source(anchor_part),
            channels=tuple(RetrievalChannel(name) for name in channel_part.split("+")),
        )


def _anchor_source(value: str) -> AnchorSource:
    if value not in ("target_topic", "target_title", "target_keyword", "edited"):
        raise ValueError("not an anchor source")
    return value  # type: ignore[return-value]


def is_canonical_decision_class(value: str) -> bool:
    """Cheap shape check for a stored metrics key (no enum lookup)."""
    return _CANONICAL.fullmatch(value) is not None


# --------------------------------------------------------------------------- #
# Eval datasets: suites, cases, provenance, privacy (evals/README.md 100-151, frozen)
# --------------------------------------------------------------------------- #
class EvalSuite(StrEnum):
    """Every suite the runner knows. The first seven are the TRD section 19 minimums.

    **Correction to ADR-0016 section 7** (its section 15.1.3 records it): these suites are NOT
    "promoted from recorded fixtures". All eight existing ``evals/suites/*/`` directories hold
    only a 24-25 line ``README.md``, so EVERY case has to be authored. That is where the
    phase's effort belongs, and it is the draft's largest effort mis-estimate.

    TRD section 19 is the SUITE list; the per-decision-class metrics live in TRD section 20
    (``evals/README.md`` section 5 sources them from there). Phase 9 should look in section 20.
    """

    DESTINATION_SELECTION = "destination-selection"
    ANCHOR_SELECTION = "anchor-selection"
    RELATIONSHIP_DETECTION = "relationship-detection"
    CRITIC = "critic"
    NO_ACTION = "no-action"
    PROTECTED_CONTENT = "protected-content"
    STALE_PAGE_VERSION = "stale-page-version"
    POLICY_REGRESSION = "policy-regression"
    """Exists as a directory; Phase 8 populates it."""
    REFLECTION_GROUNDING = "reflection-grounding"
    """Every number in a proposal's evidence must re-derive from the DB snapshot. The
    anti-fabrication gate."""
    PROPOSAL_SAFETY = "proposal-safety"
    """A frozen-field proposal is refused; 'promote yourself' / 'set auto_approve' produce a
    refusal, never a proposal. The anti-injection gate."""
    ROLLBACK_CORRECTNESS = "rollback-correctness"
    """ADR-0018 4: a failed verification must roll back to the byte-identical original, an
    already-gone link must not turn into an error, and the retry budget plus cool-down must stop
    repeated unsafe retries. Added by the final-completion directive (section 17)."""
    AUTONOMY_CLASSIFICATION = "autonomy-classification"
    """ADR-0018 1: the right decision (BLOCK / AUTO_PUBLISH / AUTO_PUBLISH_WITH_EXTRA_VERIFICATION)
    for a set of facts, in every mode. Its central case: a critic or model that says ACCEPT on a
    plan with a deterministic block must still yield BLOCK. Added by the final-completion
    directive (section 17)."""
    EDITORIAL_AGREEMENT = "editorial-agreement"
    """Deferred (ADR-0016 15.3): it needs held-out human decisions that do not exist yet."""


HARD_FAIL_SUITES: Final = frozenset(
    {
        EvalSuite.PROTECTED_CONTENT,
        EvalSuite.STALE_PAGE_VERSION,
        EvalSuite.PROPOSAL_SAFETY,
        EvalSuite.REFLECTION_GROUNDING,
        EvalSuite.ROLLBACK_CORRECTNESS,
        EvalSuite.AUTONOMY_CLASSIFICATION,
    }
)
"""Suites that must be CLEAN for any gate to pass, and that the gate policy cannot waive: the
YAML may only add to this set (:class:`GatePolicy`), never remove from it, and a test asserts
the removal is refused."""
DEFERRED_SUITES: Final = frozenset({EvalSuite.EDITORIAL_AGREEMENT})
"""Declared in the vocabulary, not populated in Phase 8."""


class CaseOrigin(StrEnum):
    SYNTHETIC = "synthetic"
    ANONYMIZED_FROM_REVIEW = "anonymized_from_review"
    REGRESSION_FROM_INCIDENT = "regression_from_incident"


class RecordedModelResponse(FrozenModel):
    """A response replayed by ``MockModelProvider``, keyed by ROLE and never by model name:
    a dataset must not pin a model, or changing ``MODEL_REASONING`` would invalidate it."""

    role: ModelRole
    request_fingerprint: str = Field(
        min_length=1, max_length=120, description="Hash of the normalised request."
    )
    response: Any = Field(description="Recorded structured output, or raw text.")


class EvalCaseInputs(FrozenModel):
    """Everything needed to replay the case offline. Paths are dataset-relative."""

    source_page: str | None = Field(
        default=None, max_length=512, description="Recorded page snapshot under the suite."
    )
    source_content_version: str | None = Field(default=None, max_length=120)
    candidates: str | None = Field(
        default=None, max_length=512, description="Recorded candidate set, when consumed."
    )
    policy_version: str | None = Field(default=None, max_length=120)
    recorded_model_responses: tuple[RecordedModelResponse, ...] = ()
    extra: dict[str, Any] = Field(
        default_factory=dict, description="Suite-specific inputs; never secrets."
    )


class EvalExpectation(FrozenModel):
    """``expected``: one correct decision, the alternatives that also pass, the hard fails."""

    decision: str = Field(min_length=1, max_length=200)
    acceptable_alternatives: tuple[str, ...] = Field(default=(), max_length=50)
    must_not: tuple[str, ...] = Field(
        default=(), max_length=50, description="Zero tolerance: a hit fails the gate."
    )

    @model_validator(mode="after")
    def _disjoint(self) -> Self:
        passing = {self.decision, *self.acceptable_alternatives}
        if passing & set(self.must_not):
            raise ValueError("a decision cannot both pass and be a must_not")
        return self


class EvalProvenance(FrozenModel):
    origin: CaseOrigin
    created_by: str = Field(min_length=1, max_length=120)
    created_at: date
    source_refs: tuple[str, ...] = Field(
        default=(), max_length=MAX_SOURCE_REFS, description="Review id, incident id, ADR."
    )


class EvalPrivacy(FrozenModel):
    """``evals/README.md`` lines 138-151: no real production data without explicit user
    approval, recorded as ``approval_ref``. Load-bearing, because a later suite may be built
    from real ``review_actions``."""

    contains_production_data: bool = False
    approval_ref: str | None = Field(
        default=None, max_length=200, description="Required when the flag above is true."
    )

    @model_validator(mode="after")
    def _approved(self) -> Self:
        if self.contains_production_data and not self.approval_ref:
            raise ValueError("production-derived data needs an explicit approval_ref")
        if not self.contains_production_data and self.approval_ref:
            raise ValueError("approval_ref without production data is meaningless")
        return self


class EvalCase(FrozenModel):
    """One dataset case: the ``evals/README.md`` lines 100-137 skeleton, frozen in full.

    Datasets are FILES (``evals/datasets/<suite>/<dataset>.v<N>.jsonl`` plus a per-dataset
    ``MANIFEST.yaml``), runs are ROWS: a fixture belongs in a reviewable commit, while
    PostgreSQL is the system of record for what a run measured. Choosing JSONL over the
    README's YAML skeleton is a Phase 8 decision the README deferred here, not a reversal.
    """

    case_id: str = Field(pattern=CASE_ID_PATTERN, description="Stable; never reused.")
    suite: EvalSuite
    fixture_version: int = Field(ge=1, description="Bump on any change to inputs or expected.")
    decision_class: DecisionClass | None = Field(
        default=None,
        description="For TRD section 20 per-class metrics. None where the case is not a "
        "link decision (proposal-safety, reflection-grounding).",
    )
    description: DescriptionText
    inputs: EvalCaseInputs
    expected: EvalExpectation
    rationale: RationaleText = Field(description="Why the expected decision is correct; human.")
    provenance: EvalProvenance
    privacy: EvalPrivacy = Field(default_factory=EvalPrivacy)

    @model_validator(mode="after")
    def _class_required_for_link_suites(self) -> Self:
        needs_class = {
            EvalSuite.DESTINATION_SELECTION,
            EvalSuite.ANCHOR_SELECTION,
            EvalSuite.CRITIC,
            EvalSuite.EDITORIAL_AGREEMENT,
            EvalSuite.POLICY_REGRESSION,
        }
        if self.suite in needs_class and self.decision_class is None:
            raise ValueError(f"{self.suite.value} cases carry a decision_class")
        return self


class DatasetManifest(FrozenModel):
    """``evals/datasets/<suite>/MANIFEST.yaml``: what a run pinned, byte for byte."""

    suite: EvalSuite
    dataset: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    dataset_version: int = Field(ge=1)
    files: dict[str, str] = Field(min_length=1, description="Dataset-relative path -> sha256.")
    case_count: int = Field(ge=1)
    provenance: str = Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)
    contains_production_data: bool = False
    approval_ref: str | None = None

    @field_validator("files")
    @classmethod
    def _shas(cls, value: dict[str, str]) -> dict[str, str]:
        for digest in value.values():
            if not re.fullmatch(SHA256_PATTERN, digest):
                raise ValueError("a manifest entry is a sha256 hex digest")
        return value


# --------------------------------------------------------------------------- #
# Evaluation: modes, metrics, the gate
# --------------------------------------------------------------------------- #
class EvaluationMode(StrEnum):
    OFFLINE = "offline"
    SHADOW = "shadow"
    CANARY = "canary"
    """Stage defined, machinery deferred -- see ADR-0016 section 16 (16.3)."""


class GateResult(StrEnum):
    PASS = "pass"  # noqa: S105 -- a gate verdict, not a secret
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    """Not enough data to judge -- below :data:`MIN_SAMPLE_COUNT` in a decision class, or a
    declared suite produced no cases. NEVER promotable: ``inconclusive`` is not ``pass``."""


class GateReasonCode(StrEnum):
    """Why a gate did not pass. Fixed vocabulary, so the UI never shows prose from a model."""

    HARD_FAIL_SUITE = "hard_fail_suite"
    MUST_NOT_VIOLATION = "must_not_violation"
    BELOW_MIN_PASS_RATE = "below_min_pass_rate"  # noqa: S105 -- a reason code
    REGRESSION_VS_BASELINE = "regression_vs_baseline"
    SUITE_NOT_RUN = "suite_not_run"
    SAMPLE_TOO_SMALL = "sample_too_small"
    BEHAVIOURAL_SIGNAL_NEGATIVE = "behavioural_signal_negative"
    """The only way a behavioural signal ever affects a gate: it BLOCKS (16.7)."""
    EVIDENCE_NOT_REPRODUCIBLE = "evidence_not_reproducible"
    FROZEN_FIELD_TOUCHED = "frozen_field_touched"
    BASELINE_MISSING = "baseline_missing"


class GateReason(FrozenModel):
    code: GateReasonCode
    suite: EvalSuite | None = None
    decision_class: str | None = Field(default=None, pattern=DECISION_CLASS_PATTERN)
    detail: NoteText = ""
    """A rendered sentence from the code above. Never model output, never exception text."""


class SuiteMetrics(FrozenModel):
    suite: EvalSuite
    cases: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    must_not_violations: int = Field(ge=0)
    pass_rate: float | None = Field(
        default=None, ge=0.0, le=1.0, description="None when the suite produced no cases."
    )
    baseline_pass_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    precision_at_1: float | None = Field(default=None, ge=0.0, le=1.0)
    recall_at_3: float | None = Field(default=None, ge=0.0, le=1.0)
    kendall_tau: float | None = Field(default=None, ge=-1.0, le=1.0)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.passed + self.failed != self.cases:
            raise ValueError("passed + failed must equal cases")
        if (self.pass_rate is None) != (self.cases == 0):
            raise ValueError("pass_rate is null exactly when the suite ran no cases")
        return self


class DecisionClassMetrics(FrozenModel):
    """The TRD section 20 per-class row. ``positive_outcome_rate`` and the calibration fields
    are what Phase 9 needs and what the ADR draft omitted (15.6)."""

    decision_class: str = Field(pattern=DECISION_CLASS_PATTERN)
    sample_count: int = Field(ge=0)
    approval_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    edit_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    reject_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    rollback_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    positive_outcome_rate: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Share with a non-negative measured outcome. BEHAVIOURAL: it may block a "
        "gate and may never pass one.",
    )
    mean_raw_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    calibrated_confidence: float | None = Field(
        default=None, ge=0.0, le=1.0, description="None until a calibration exists (Phase 9)."
    )
    expected_calibration_error: float | None = Field(default=None, ge=0.0, le=1.0)
    reliability_bins: tuple[tuple[float, float, int], ...] = Field(
        default=(), max_length=20, description="(bin_upper, observed_rate, n). Reported only."
    )
    sufficient: bool = Field(
        description="sample_count >= the policy's min_sample_count. False forces inconclusive."
    )


class EvaluationMetrics(FrozenModel):
    """``policy_evaluations.metrics``. ``by_decision_class`` is REQUIRED (15.7.11): keying it
    by the canonical decision-class form is what lets Phase 9 read calibration without a
    schema change."""

    cases_total: int = Field(ge=0)
    cases_passed: int = Field(ge=0)
    by_suite: tuple[SuiteMetrics, ...] = Field(min_length=1)
    by_decision_class: dict[str, DecisionClassMetrics] = Field(
        description="Canonical decision class -> its metrics. May be empty, never absent."
    )

    @model_validator(mode="after")
    def _keys_agree(self) -> Self:
        for key, metrics in self.by_decision_class.items():
            if not is_canonical_decision_class(key):
                raise ValueError(f"{key!r} is not a canonical decision class")
            if metrics.decision_class != key:
                raise ValueError("a by_decision_class key must equal its row's decision_class")
        suites = [row.suite for row in self.by_suite]
        if len(set(suites)) != len(suites):
            raise ValueError("one SuiteMetrics row per suite")
        if sum(row.cases for row in self.by_suite) != self.cases_total:
            raise ValueError("cases_total must equal the sum over suites")
        if sum(row.passed for row in self.by_suite) != self.cases_passed:
            raise ValueError("cases_passed must equal the sum over suites")
        return self


# --------------------------------------------------------------------------- #
# The gate policy (policies/learning/learning.v<N>.yaml)
# --------------------------------------------------------------------------- #
class SuiteGate(FrozenModel):
    min_pass_rate: float = Field(ge=0.0, le=1.0)
    max_regression_vs_baseline: float = Field(
        ge=0.0, le=1.0, description="Largest tolerated drop in pass rate against the baseline."
    )


class GatePolicy(FrozenModel):
    """What ``gate_result = pass`` requires. A hard-fail suite cannot be waived."""

    hard_fail_suites: tuple[EvalSuite, ...] = Field(min_length=1)
    suites: dict[EvalSuite, SuiteGate] = Field(min_length=1)
    min_sample_count: int = Field(
        default=MIN_SAMPLE_COUNT,
        ge=MIN_SAMPLE_COUNT,
        description="Per decision class. The floor may be RAISED and never lowered (15.4).",
    )
    promotion_max_eval_age_days: int = Field(default=14, ge=1, le=365)
    shadow_min_runs: int = Field(default=3, ge=1, le=100)
    shadow_min_plans: int = Field(default=20, ge=1, le=100_000)
    required_stages: dict[str, tuple[ProposalStatus, ...]] = Field(
        description="Proposal kind or target kind -> the MINIMUM stages before promotion "
        "(ADR-0016 15.3). Keys: 'ranking', 'recommendation', 'agents', 'skill'."
    )

    @field_validator("hard_fail_suites")
    @classmethod
    def _cannot_waive(cls, value: tuple[EvalSuite, ...]) -> tuple[EvalSuite, ...]:
        missing = HARD_FAIL_SUITES - set(value)
        if missing:
            raise ValueError(
                "a hard-fail suite cannot be waived by policy; missing "
                + ", ".join(sorted(suite.value for suite in missing))
            )
        if len(set(value)) != len(value):
            raise ValueError("hard_fail_suites must be unique")
        return value

    @model_validator(mode="after")
    def _hard_fail_suites_are_declared(self) -> Self:
        missing = set(self.hard_fail_suites) - set(self.suites)
        if missing:
            raise ValueError(
                "every hard-fail suite needs a declared gate; missing "
                + ", ".join(sorted(suite.value for suite in missing))
            )
        allowed = {kind.value for kind in TargetKind} | {ProposalKind.SKILL.value}
        unknown = set(self.required_stages) - allowed
        if unknown:
            raise ValueError("required_stages keys: " + ", ".join(sorted(allowed)))
        if set(self.required_stages) != allowed:
            raise ValueError("required_stages must declare every kind")
        for key, stages in self.required_stages.items():
            if not stages:
                raise ValueError(f"required_stages[{key}] must name at least one stage")
            if ProposalStatus.OFFLINE_TEST not in stages:
                raise ValueError(f"required_stages[{key}] must include offline_test")
            if set(stages) - OPEN_PROPOSAL_STATUSES:
                raise ValueError(f"required_stages[{key}] names a non-open stage")
            if len(set(stages)) != len(stages):
                raise ValueError(f"required_stages[{key}] must be unique")
        return self


class ChangePolicy(FrozenModel):
    """The narrowing-only path sets. The YAML may take away and can never add (15.7.5)."""

    allowed_paths: tuple[str, ...] = Field(min_length=1)
    frozen_paths: tuple[str, ...] = Field(min_length=1)

    @field_validator("allowed_paths")
    @classmethod
    def _subset_of_the_contract(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        widened = sorted(set(value) - ALLOWED_CHANGE_PATHS)
        if widened:
            raise ValueError(
                "the learning policy may only NARROW the change allowlist; these paths are "
                "not in ken_contracts.learning.ALLOWED_CHANGE_PATHS: " + ", ".join(widened)
            )
        frozen = sorted(path for path in value if is_frozen_path(path))
        if frozen:
            raise ValueError("a frozen field can never be allowlisted: " + ", ".join(frozen))
        if len(set(value)) != len(value):
            raise ValueError("allowed_paths must be unique")
        return value

    @field_validator("frozen_paths")
    @classmethod
    def _superset_of_the_contract(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        missing = sorted(FROZEN_POLICY_PATHS - set(value))
        if missing:
            raise ValueError(
                "the learning policy may only ADD frozen paths; these are missing: "
                + ", ".join(missing)
            )
        if len(set(value)) != len(value):
            raise ValueError("frozen_paths must be unique")
        return value


class ExperimentPolicy(FrozenModel):
    """Experiment defaults. The HOLDOUT fraction is the only one with teeth: the hard maximum
    is a database CHECK, so this field can lower it and never raise it."""

    holdout_treatment_fraction: float = Field(
        default=DEFAULT_HOLDOUT_TREATMENT_FRACTION,
        gt=0.0,
        le=MAX_HOLDOUT_TREATMENT_FRACTION,
        description="Share of TARGET pages a holdout withholds inbound links from. Capped at "
        "0.20 by the contract and again by a DB CHECK; activating one at all needs "
        "Settings.experiments_allow_holdout.",
    )
    policy_ab_treatment_fraction: float = Field(
        default=0.5, gt=0.0, le=1.0, description="Both arms get links, so this is unconstrained."
    )
    min_pages_per_arm: int = Field(
        default=10, ge=1, le=10_000, description="Below this an activation is refused."
    )


class LearningPolicy(FrozenModel):
    """``policies/learning/learning.v<N>.yaml``, served by ``/settings/policies``.

    DATA, not code: gates, sample floors, the required stage path per kind and the (narrowed)
    change allowlist. It cannot widen what the contract allows, cannot waive a hard-fail suite
    and cannot lower the sample floor -- the three ways a human-editable document could
    otherwise undo section 15's safety work.
    """

    policy: Literal["learning"]
    version: int = Field(ge=1)
    status: str = Field(min_length=1, max_length=64)
    source: str = Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)
    gates: GatePolicy
    changes: ChangePolicy
    experiments: ExperimentPolicy = Field(default_factory=ExperimentPolicy)
    behavioural_may_block_only: Literal[True] = Field(
        default=True,
        description="Pinned at the contract level (the PoliciesView.auto_approve_enabled "
        "pattern): a behavioural signal may block a gate and may never pass one.",
    )
    search_is_diagnostic_only: Literal[True] = True
    """Search Console data never gates anything, in any configuration (ADR-0016 section 2)."""


# --------------------------------------------------------------------------- #
# Evidence and the bounded model role (15.2d / 15.7.6 / 15.7.7)
# --------------------------------------------------------------------------- #
class EvidenceWindow(FrozenModel):
    start: date
    end: date

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end < self.start:
            raise ValueError("end precedes start")
        return self


class DecisionClassObservation(FrozenModel):
    """One row of the aggregate table. EVERY number here is computed in SQL/Python by
    ``ken_services.learning.aggregate``; a static test asserts no other module builds
    evidence, and no model-produced number may appear in it."""

    decision_class: str = Field(pattern=DECISION_CLASS_PATTERN)
    sample_count: int = Field(ge=0)
    approvals: int = Field(ge=0)
    edits: int = Field(ge=0)
    rejections: int = Field(ge=0)
    rollbacks: int = Field(ge=0)
    positive_outcomes: int = Field(ge=0)
    approval_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    edit_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    reject_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    rollback_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    positive_outcome_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    reason_histogram: dict[str, int] = Field(
        default_factory=dict, description="Structured review reason code -> count."
    )
    anchor_edit_char_delta: int = Field(
        default=0, description="Net characters humans added to or removed from anchors."
    )
    link_totals: dict[AnalyticsSource, MetricTotals] = Field(
        default_factory=dict, description="ADVISORY (behavioural): may block, never pass."
    )
    behavioural_flags: tuple[ConfounderFlag, ...] = ()
    sufficient: bool = Field(description="sample_count >= the policy's min_sample_count.")

    @model_validator(mode="after")
    def _counts_fit(self) -> Self:
        if self.approvals + self.rejections > self.sample_count:
            raise ValueError("approvals + rejections cannot exceed sample_count")
        for name in ("edits", "rollbacks", "positive_outcomes"):
            if getattr(self, name) > self.sample_count:
                raise ValueError(f"{name} cannot exceed sample_count")
        return self


class ProposalOption(FrozenModel):
    """One SERVER-ENUMERATED option. The model picks an index; it never emits a number that is
    stored and never names a field, so it cannot reach a frozen path."""

    index: int = Field(ge=0, le=MAX_OPTIONS_PER_PROPOSAL - 1)
    label: str = Field(min_length=1, max_length=120, description="Rendered from enum codes.")
    changes: tuple[PolicyChange, ...] = Field(
        default=(), max_length=MAX_CHANGES_PER_PROPOSAL, description="Empty for 'do nothing'."
    )


class ProposalEvidence(FrozenModel):
    """``learning_proposals.evidence``: the observation window, every count, the policy and
    engine versions in scope, and the FULL option set the model chose from (15.7.7), so the
    choice is reconstructable long after the run."""

    window: EvidenceWindow
    generated_at: AwareDatetime
    aggregate_version: str = Field(pattern=VERSION_PATTERN)
    observations: tuple[DecisionClassObservation, ...] = Field(max_length=MAX_OBSERVATIONS)
    options: tuple[ProposalOption, ...] = Field(min_length=1, max_length=MAX_OPTIONS_PER_PROPOSAL)
    chosen_index: int = Field(ge=0)
    sample_count_total: int = Field(ge=0)
    policy_versions: dict[str, str] = Field(
        description="Policy kind -> the version in scope (ranking, recommendation, agents, "
        "taxonomy, analytics, learning)."
    )
    engine_version: str = Field(min_length=1, max_length=120)
    analysis_version: str = Field(min_length=1, max_length=120)
    model: str | None = Field(
        default=None, description="Resolved REASONING model, or None when none answered."
    )
    fallback_used: bool = False
    fallback_reason: FallbackReason | None = None

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        indices = [option.index for option in self.options]
        if indices != list(range(len(self.options))):
            raise ValueError("options must be indexed 0..n-1, in order")
        if self.chosen_index >= len(self.options):
            raise ValueError("chosen_index is outside the recorded option set")
        classes = [observation.decision_class for observation in self.observations]
        if len(set(classes)) != len(classes):
            raise ValueError("one observation per decision class")
        if sum(observation.sample_count for observation in self.observations) != (
            self.sample_count_total
        ):
            raise ValueError("sample_count_total must equal the sum over observations")
        if (self.fallback_reason is not None) and not self.fallback_used:
            raise ValueError("a fallback reason without a fallback")
        if self.fallback_used and self.model is not None:
            raise ValueError("a fallback means no model answer was used")
        return self


class ProposalDraft(FrozenModel):
    """The ONLY thing the REASONING role may return (ADR-0016 section 3 as amended).

    The model chooses and explains; ``choice_index`` is validated here against
    ``option_count``, the live length of the server-enumerated option set, so an out-of-range
    or invented index is a schema violation that falls back to option 0 like every other agent
    node. Nothing in this model is a stored number, a field name, a path or a URL.

    The prompt that produces it carries ENUM CODES, INTEGERS AND FLOATS ONLY -- no review note,
    no anchor string, no title, no URL (15.7.6). Anchor text IS page content, and reflection is
    corpus-level, so one poisoned anchor would steer every proposal; that blast radius is
    strictly worse than the per-page agent prompt's.
    """

    title: TitleText
    rationale: ModelText
    choice_index: int = Field(ge=0)
    option_count: int = Field(ge=1, le=MAX_OPTIONS_PER_PROPOSAL)

    @model_validator(mode="after")
    def _index_within_the_live_option_set(self) -> Self:
        if self.choice_index >= self.option_count:
            raise ValueError(
                f"choice_index {self.choice_index} is outside the {self.option_count} options "
                "offered"
            )
        return self


# --------------------------------------------------------------------------- #
# Skills
# --------------------------------------------------------------------------- #
class SkillStatus(StrEnum):
    """``skills.status`` (ADR-0016 section 4). Shorter than the policy ladder on purpose:
    nothing consumes a skill at runtime in Phase 8, so a shadow stage would be theatre."""

    DRAFT = "draft"
    """The row written when a ``skill`` proposal is created."""
    OFFLINE_TEST = "offline_test"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    RETIRED = "retired"
    """Replaced by a newer promoted version."""


SKILL_TRANSITIONS: Final[Mapping[SkillStatus, frozenset[SkillStatus]]] = {
    SkillStatus.DRAFT: frozenset({SkillStatus.OFFLINE_TEST, SkillStatus.REJECTED}),
    SkillStatus.OFFLINE_TEST: frozenset({SkillStatus.PROMOTED, SkillStatus.REJECTED}),
    SkillStatus.PROMOTED: frozenset({SkillStatus.RETIRED}),
    SkillStatus.REJECTED: frozenset(),
    SkillStatus.RETIRED: frozenset(),
}
"""``proposed -> offline_test -> promoted``, plus ``rejected`` and ``retired``. The proposal's
own ladder (:data:`PROPOSAL_TRANSITIONS`) governs the audit trail; this is the registry row."""


class SkillSummary(FrozenModel):
    id: UUID
    name: str = Field(pattern=SKILL_NAME_PATTERN)
    version: int = Field(ge=1)
    description: str
    status: SkillStatus
    source: SkillSource
    proposal_id: UUID | None = None
    evaluation_run_id: UUID | None = None
    evaluation_score: float | None = Field(
        default=None,
        description="Display scalar only. The real record is the policy_evaluations run and "
        "its per-suite metric map (TRD section 17 amended, ADR-0016 13.1).",
    )
    promoted_by: str | None = None
    promoted_at: AwareDatetime | None = None
    created_at: AwareDatetime


class SkillDetail(SkillSummary):
    instructions: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    suites: tuple[EvalSuite, ...]
    model_role: ModelRole
    versions: tuple[SkillSummary, ...] = Field(
        default=(), description="Every version of this name, newest first."
    )


# --------------------------------------------------------------------------- #
# Experiments (owed by ADR-0015 12.8; plumbing only -- ADR-0016 16.6)
# --------------------------------------------------------------------------- #
class ExperimentKind(StrEnum):
    """What an experiment varies. Both kinds exist; ``holdout`` is DEFAULTED OFF, not omitted.

    ADR-0016 16.4 cut ``holdout`` entirely; the user has since decided it is built and gated
    instead. Withholding links from live pages is a real editorial and SEO cost, so the safety
    is three independent gates rather than a missing enum member:

    1. ``treatment_fraction <= 0.20`` for a ``holdout`` -- a hard maximum, CHECKed in the
       database as well as here. The policy default is
       :data:`DEFAULT_HOLDOUT_TREATMENT_FRACTION` (0.10).
    2. ``Settings.experiments_allow_holdout`` defaults ``False``, and
       ``ExperimentLifecycle.activate`` refuses ``draft -> active`` for a ``holdout`` with
       :attr:`ExperimentRefusal.HOLDOUT_NOT_ENABLED` while it is unset.
    3. The same transaction must hold the transaction-local
       :data:`EXPERIMENT_ACTIVATOR_SETTING`, which only that one service sets -- so a raw
       ``UPDATE experiments SET status = 'active'`` cannot start a holdout either.
    """

    POLICY_AB = "policy_ab"
    """Both arms get links; only the ranking policy differs, resolved per source from the arm.
    Locks nobody. Requires a ``policy_candidates`` row."""
    HOLDOUT = "holdout"
    """Control pages are deliberately WITHHELD inbound links, so their search performance can
    be compared with treatment pages. Assigned by TARGET page (see
    :class:`ExperimentUnit`), carries no candidate, and is the only kind
    :meth:`~ken_services.learning.protocol.SqlExperimentGuard.locked` ever locks."""


class ExperimentUnit(StrEnum):
    """Which page an arm is assigned to -- determined by the kind, never chosen freely.

    This resolves the arm-contamination bug the pre-fan-out review found (ADR-0016 15.5): the
    draft assigned arms by SOURCE page while saying a holdout's comparison is the TARGET page's
    metric. Those are different pages, so the arms were contaminated -- nothing stopped a
    control page from receiving inbound links from an unassigned source.

    Resolution (a) of the two the user offered: **a holdout assigns by TARGET page**, and
    ``locked()`` refuses any plan whose TARGET is a control page. That is coherent with the
    existing ``ExperimentGuard.locked(*, source_page_id, target_page_id)`` signature, which
    already takes both pages, and it is the question a holdout is actually for -- "withhold
    inbound links from these pages and compare their search performance".
    """

    SOURCE_PAGE = "source_page"
    """``policy_ab``: the arm picks which ranking policy ranks that source's candidates."""
    TARGET_PAGE = "target_page"
    """``holdout``: the arm decides whether that page may receive new inbound links."""


EXPERIMENT_UNIT_BY_KIND: Final[Mapping[ExperimentKind, ExperimentUnit]] = {
    ExperimentKind.POLICY_AB: ExperimentUnit.SOURCE_PAGE,
    ExperimentKind.HOLDOUT: ExperimentUnit.TARGET_PAGE,
}
"""The unit is a FUNCTION of the kind (a DB CHECK pins the pair), so no experiment can be
created whose comparison is about one page while its arms are assigned to another."""


def experiment_unit(kind: ExperimentKind) -> ExperimentUnit:
    """The assignment unit of ``kind``. Total over :class:`ExperimentKind`."""
    return EXPERIMENT_UNIT_BY_KIND[ExperimentKind(kind)]


class ExperimentStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    """A ``policy_ab`` may be activated freely (both arms get links). A ``holdout`` may not,
    unless ``Settings.experiments_allow_holdout`` is on AND the activating transaction holds
    :data:`EXPERIMENT_ACTIVATOR_SETTING`."""
    ENDED = "ended"


EXPERIMENT_TRANSITIONS: Final[Mapping[ExperimentStatus, frozenset[ExperimentStatus]]] = {
    ExperimentStatus.DRAFT: frozenset({ExperimentStatus.ACTIVE, ExperimentStatus.ENDED}),
    ExperimentStatus.ACTIVE: frozenset({ExperimentStatus.ENDED}),
    ExperimentStatus.ENDED: frozenset(),
}


class ExperimentArm(StrEnum):
    CONTROL = "control"
    TREATMENT = "treatment"


class ExperimentAssignmentView(FrozenModel):
    """Deterministic AND materialised: the arm comes from the first 8 bytes of
    ``sha256(key + ':' + str(page_id))`` against ``treatment_fraction``, and the row is
    WRITTEN, so the arm stays auditable even if the hash function ever changes.

    ``page_id`` is the source page for a ``policy_ab`` and the TARGET page for a ``holdout``
    (:class:`ExperimentUnit`)."""

    experiment_id: UUID
    page_id: UUID
    arm: ExperimentArm
    assigned_by: str
    assigned_at: AwareDatetime


class ExperimentView(FrozenModel):
    id: UUID
    key: str = Field(pattern=EXPERIMENT_KEY_PATTERN)
    description: str
    unit: ExperimentUnit
    kind: ExperimentKind
    status: ExperimentStatus
    candidate_id: UUID | None = Field(
        default=None, description="Required for policy_ab, forbidden for holdout."
    )
    proposal_id: UUID | None = None
    treatment_fraction: float = Field(gt=0.0, le=1.0)
    created_by: str
    created_at: AwareDatetime
    updated_at: AwareDatetime
    started_at: AwareDatetime | None = None
    ended_at: AwareDatetime | None = None
    control_pages: int = Field(default=0, ge=0)
    treatment_pages: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _kind_determines_unit_candidate_and_cap(self) -> Self:
        if self.unit is not experiment_unit(self.kind):
            raise ValueError(
                f"a {self.kind.value} experiment assigns arms by {experiment_unit(self.kind).value}"
            )
        if self.kind is ExperimentKind.POLICY_AB and self.candidate_id is None:
            raise ValueError("a policy_ab experiment names the candidate it tests")
        if self.kind is ExperimentKind.HOLDOUT:
            if self.candidate_id is not None:
                raise ValueError("a holdout tests no candidate policy")
            if self.treatment_fraction > MAX_HOLDOUT_TREATMENT_FRACTION:
                raise ValueError(
                    "a holdout may withhold links from at most "
                    f"{MAX_HOLDOUT_TREATMENT_FRACTION:.0%} of pages"
                )
        return self


class ExperimentDetail(ExperimentView):
    assignments: tuple[ExperimentAssignmentView, ...] = ()
    comparison: tuple[DecisionClassMetrics, ...] = Field(
        default=(),
        description="Per-arm editorial comparison. PERMITTED claim: 'observed difference "
        "between arms over [interval], N_control/N_treatment pages, with these confounder "
        "flags'. FORBIDDEN: significance, causal lift, revenue, ranking causation.",
    )


# --------------------------------------------------------------------------- #
# Evaluation runs (policy_evaluations) and policy candidates
# --------------------------------------------------------------------------- #
class EvaluationSummary(FrozenModel):
    id: UUID
    job_run_id: UUID
    mode: EvaluationMode
    proposal_id: UUID | None = None
    candidate_id: UUID | None = None
    skill_id: UUID | None = None
    proposal_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    suites: tuple[EvalSuite, ...]
    gate_result: GateResult
    baseline_evaluation_id: UUID | None = None
    runner_version: str
    model_provider: str
    git_sha: str | None = None
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None


class EvaluationDetail(EvaluationSummary):
    """``evaluation_ref`` is ``str(policy_evaluations.id)``; the promotion service accepts
    nothing else."""

    dataset_manifests: dict[str, str] = Field(
        description="Dataset name -> manifest sha256, so a run is reproducible from a checkout."
    )
    metrics: EvaluationMetrics
    gate_reasons: tuple[GateReason, ...] = ()
    model_role_models: dict[ModelRole, str] = Field(
        default_factory=dict, description="The RESOLVED model per role at run time."
    )
    seed: int | None = None
    policy_shas: dict[str, str] = Field(default_factory=dict)


class PolicyCandidateRef(FrozenModel):
    """Read-safe reference to a stored candidate: identity and digest, never the document.

    The DOCUMENT is a policy document, and ADR-0013 reserves policy documents to
    ``operator_admin`` through ``read_settings``; exposing a candidate under ``read`` would be
    a permission downgrade. :class:`PolicyCandidateView` carries the document and is served
    only behind ``read_settings``.
    """

    id: UUID
    proposal_id: UUID
    policy_kind: TargetKind
    base_version: str
    candidate_version: str
    document_sha256: str = Field(pattern=SHA256_PATTERN)
    created_at: AwareDatetime


class PolicyCandidateView(PolicyCandidateRef):
    """``read_settings`` ONLY. The full validated candidate document, so ``offline_test`` is
    replayable without a file on disk."""

    document: dict[str, Any] = Field(description="The validated candidate policy document.")


# --------------------------------------------------------------------------- #
# Proposal read models and the one write request
# --------------------------------------------------------------------------- #
class ProposalTransitionView(FrozenModel):
    """One append-only ``learning_proposal_transitions`` row. This table, plus its
    same-transaction ``txid`` guard, is where "no self-promotion" is enforced."""

    id: int
    proposal_id: UUID
    from_status: ProposalStatus
    to_status: ProposalStatus
    actor: str
    reason: ProposalRejectionReason | None = None
    note: str | None = Field(default=None, max_length=MAX_NOTE_CHARS)
    evaluation_run_id: UUID | None = None
    created_at: AwareDatetime


class ProposalSummary(FrozenModel):
    id: UUID
    kind: ProposalKind
    status: ProposalStatus
    target_kind: TargetKind | None = Field(default=None, description="None for a skill.")
    base_version: str
    title: str
    fingerprint: str = Field(pattern=SHA256_PATTERN)
    proposed_by: str = Field(description="Always ``system:`` or ``agent:`` (a DB CHECK).")
    job_run_id: UUID
    model: str | None = None
    fallback_used: bool = False
    supersedes: UUID | None = Field(
        default=None, description="Set only by a human (15.3 cut automatic superseding)."
    )
    created_at: AwareDatetime
    updated_at: AwareDatetime


class PromotionPreview(FrozenModel):
    """What the human must see before promoting (ADR-0016 section 10), computed, never stored.

    ``target_paths`` holds EVERY file a promotion would write. For ``ranking`` that is TWO
    files and the write is atomic (15.7.8): the active ranking policy is resolved from
    ``policies/recommendation/recommendation.v1.yaml``'s ``ranking_policy:`` pointer by
    ``resolve_ranking_policy``, NOT from ``Settings`` -- so promoting weights also requires a
    new recommendation document. Only the CLI writes a policy file; the API records the event.
    """

    target_paths: tuple[str, ...] = Field(
        min_length=1, description="Repository-relative paths the CLI would write."
    )
    next_version: str
    rollback_to_version: str | None = Field(
        default=None, description="The version a rollback would restore."
    )
    requires_operator_restart: bool = Field(
        default=True,
        description="Promotion changes the active policy path and Settings is frozen, so it "
        "needs a restart. Hot reload would be a material architecture change (16, open).",
    )
    blocked: bool = Field(description="True while any refusal below applies.")
    blocked_by: tuple[PromotionRefusal, ...] = Field(
        default=(), description="Empty only when the button may be enabled."
    )


class ProposalDetail(ProposalSummary):
    rationale: str
    changes: tuple[PolicyChange, ...] = Field(
        default=(), description="The exact parameter diff; empty for a skill proposal."
    )
    frozen_fields_untouched: Literal[True] = Field(
        default=True,
        description="A contract-level assertion, not a computed opinion: a proposal touching "
        "a frozen path cannot be constructed, so a stored one never has.",
    )
    evidence: ProposalEvidence
    transitions: tuple[ProposalTransitionView, ...]
    evaluations: tuple[EvaluationSummary, ...] = ()
    candidate: PolicyCandidateRef | None = None
    skill: SkillSummary | None = None
    promotion: PromotionPreview | None = None
    required_stages: tuple[ProposalStatus, ...] = Field(
        default=(), description="The minimum path the gate policy requires for this kind."
    )
    stages_completed: tuple[ProposalStatus, ...] = Field(default=())


class CorpusBanner(FrozenModel):
    """The banner every ``/learning`` screen carries (ADR-0016 15.4 iii / 16.1). Honest by
    construction: the numbers come from the database, and the refusal is a mechanism."""

    demonstration_mode: bool = Field(
        description="Settings.learning_demonstration_mode. True: no promotion can succeed."
    )
    pages_total: int = Field(ge=0)
    plans_total: int = Field(ge=0)
    review_actions_total: int = Field(ge=0)
    min_sample_count: int = Field(ge=MIN_SAMPLE_COUNT)
    note: str = Field(
        description="A fixed sentence naming the corpus size and that nothing on this data "
        "may be promoted. Never model output."
    )


class LearningOverview(FrozenModel):
    """``GET /learning``: the banner plus the counts the list screens need."""

    banner: CorpusBanner
    proposals_by_status: dict[str, int]
    skills_by_status: dict[str, int]
    evaluations_by_gate_result: dict[str, int]
    experiments_by_status: dict[str, int]
    latest_reflection_run_id: UUID | None = None
    latest_evaluation_run_id: UUID | None = None
    policy_version: int = Field(ge=1, description="The active learning policy version.")


class AdvanceProposalRequest(FrozenModel):
    """``POST /learning/proposals/{id}/advance`` -- the ONLY write route of the phase.

    The actor is NEVER in the body: it is derived from the session (``principal.actor``), as
    Phase 3 QA H3 requires and as ``PromotionService.advance`` enforces by taking a
    ``Principal``. ``operation_id`` makes a replay detectable.
    """

    operation_id: OperationId
    to_status: ProposalStatus
    evaluation_run_id: UUID | None = Field(
        default=None, description="Required for 'promoted'; refused otherwise."
    )
    reason: ProposalRejectionReason | None = None
    note: NoteText | None = None

    @model_validator(mode="after")
    def _well_formed(self) -> Self:
        if self.to_status is ProposalStatus.PROMOTED and self.evaluation_run_id is None:
            raise ValueError("promotion must cite the passing evaluation run")
        if self.to_status is not ProposalStatus.PROMOTED and self.evaluation_run_id is not None:
            raise ValueError("only a promotion carries an evaluation_run_id")
        allowed = REASONS_BY_PROPOSAL_STATUS.get(self.to_status)
        if allowed is None:
            if self.reason is not None:
                raise ValueError(f"{self.to_status.value} takes no reason")
        elif self.reason is None:
            raise ValueError(f"{self.to_status.value} requires a structured reason")
        elif self.reason not in allowed:
            raise ValueError(
                f"{self.reason.value} is not a reason for {self.to_status.value}; one of "
                + ", ".join(sorted(item.value for item in allowed))
            )
        return self


class AdvanceProposalResponse(FrozenModel):
    proposal: ProposalDetail
    transition: ProposalTransitionView
    replayed: bool = Field(
        default=False, description="True when operation_id matched an earlier call."
    )


# --------------------------------------------------------------------------- #
# Typed failures
# --------------------------------------------------------------------------- #
class PromotionRefusal(StrEnum):
    """Why :meth:`PromotionService.advance` refused. Fixed vocabulary; the message is the code,
    never exception text and never a database message."""

    DEMONSTRATION_MODE = "demonstration_mode"
    """``Settings.learning_demonstration_mode`` is set. The Phase 8 default (16.1)."""
    NOT_A_HUMAN = "not_a_human"
    """The principal's actor is not ``user:``. Unreachable through the API, checked anyway."""
    PERMISSION_DENIED = "permission_denied"
    TRANSITION_NOT_ALLOWED = "transition_not_allowed"
    REQUIRED_STAGE_MISSING = "required_stage_missing"
    EVALUATION_MISSING = "evaluation_missing"
    EVALUATION_NOT_FOUND = "evaluation_not_found"
    EVALUATION_NOT_PASSED = "evaluation_not_passed"
    """``gate_result`` is ``fail`` or ``inconclusive``."""
    EVALUATION_FOREIGN = "evaluation_foreign"
    """The run belongs to another proposal or skill."""
    FINGERPRINT_MISMATCH = "fingerprint_mismatch"
    DATASET_CHANGED = "dataset_changed"
    EVALUATION_TOO_OLD = "evaluation_too_old"
    SAMPLE_TOO_SMALL = "sample_too_small"
    BEHAVIOURAL_BLOCK = "behavioural_block"
    FROZEN_FIELD = "frozen_field"
    REASON_REQUIRED = "reason_required"
    REPLAYED_WITH_DIFFERENT_BODY = "replayed_with_different_body"
    PROMOTER_GUARD_MISSING = "promoter_guard_missing"
    """The transaction-local ``ken.learning_promoter`` setting was not established."""


class ExperimentRefusal(StrEnum):
    """Why :meth:`ExperimentLifecycle.activate` or an assignment was refused."""

    HOLDOUT_NOT_ENABLED = "holdout_not_enabled"
    """``Settings.experiments_allow_holdout`` is unset, which is the default. Withholding
    inbound links from live pages is the user's decision, so nothing activates a holdout until
    they make it."""
    FRACTION_TOO_LARGE = "fraction_too_large"
    """A holdout above :data:`MAX_HOLDOUT_TREATMENT_FRACTION`."""
    NOT_A_DRAFT = "not_a_draft"
    NO_ASSIGNMENTS = "no_assignments"
    """Activating with no materialised arms would silently measure nothing."""
    TRANSITION_NOT_ALLOWED = "transition_not_allowed"
    PAGE_ALREADY_IN_A_LIVE_EXPERIMENT = "page_already_in_a_live_experiment"
    WRONG_UNIT = "wrong_unit"
    """An assignment whose page role does not match the experiment's unit."""


class LearningError(Exception):
    """Base of learning failures."""


class ProposalNotFoundError(LearningError):
    def __init__(self) -> None:
        super().__init__("learning proposal not found")


class EvaluationNotFoundError(LearningError):
    def __init__(self) -> None:
        super().__init__("evaluation run not found")


class SkillNotFoundError(LearningError):
    def __init__(self) -> None:
        super().__init__("skill not found")


class ExperimentNotFoundError(LearningError):
    def __init__(self) -> None:
        super().__init__("experiment not found")


class ExperimentRefusedError(LearningError):
    """A refused activation or assignment. Carries the code only."""

    def __init__(self, refusal: ExperimentRefusal) -> None:
        super().__init__(refusal.value)
        self.refusal = refusal


class PromotionRefusedError(LearningError):
    """A refused advance. Carries the code only."""

    def __init__(self, refusal: PromotionRefusal) -> None:
        super().__init__(refusal.value)
        self.refusal = refusal


# --------------------------------------------------------------------------- #
# The API's only route to stored learning data (READ ONLY sessions)
# --------------------------------------------------------------------------- #
@runtime_checkable
class LearningReader(Protocol):
    """Modelled on :class:`~ken_contracts.analytics.AnalyticsReader`: every GET body delegates
    here and nothing else reaches the tables.

    ``candidate`` is the one method whose result carries a policy DOCUMENT, so its route is
    ``read_settings`` and not ``read``.
    """

    async def overview(self) -> LearningOverview: ...

    async def list_proposals(
        self,
        *,
        limit: int,
        offset: int,
        kind: ProposalKind | None = None,
        status: ProposalStatus | None = None,
        target_kind: TargetKind | None = None,
    ) -> ListResponse[ProposalSummary]: ...

    async def get_proposal(self, *, proposal_id: UUID) -> ProposalDetail: ...

    async def get_candidate(self, *, proposal_id: UUID) -> PolicyCandidateView: ...

    async def list_evaluations(
        self,
        *,
        limit: int,
        offset: int,
        mode: EvaluationMode | None = None,
        gate_result: GateResult | None = None,
        proposal_id: UUID | None = None,
    ) -> ListResponse[EvaluationSummary]: ...

    async def get_evaluation(self, *, evaluation_id: UUID) -> EvaluationDetail: ...

    async def list_skills(
        self, *, limit: int, offset: int, status: SkillStatus | None = None
    ) -> ListResponse[SkillSummary]: ...

    async def get_skill(self, *, name: str) -> SkillDetail: ...

    async def list_experiments(
        self, *, limit: int, offset: int, status: ExperimentStatus | None = None
    ) -> ListResponse[ExperimentView]: ...

    async def get_experiment(self, *, key: str) -> ExperimentDetail: ...


def fingerprint_payload(
    *,
    kind: ProposalKind,
    target_kind: TargetKind | None,
    base_version: str,
    change_paths: Sequence[str],
    change_values: Mapping[str, Any] | None = None,
    skill_name: str | None = None,
) -> dict[str, Any]:
    """The canonical object whose ``sha256(canonical_json(...))`` is a proposal fingerprint.

    Defined here so the reflection job, the dedupe query and the promotion check all hash the
    SAME object. Policies hash (kind, target kind, base version, sorted changes); skills hash
    (name, normalised instructions, input schema, output schema) -- the caller passes the
    normalised skill parts through ``change_values``.
    """
    return {
        "kind": kind.value,
        "target_kind": None if target_kind is None else target_kind.value,
        "base_version": base_version,
        "change_paths": sorted(change_paths),
        "change_values": dict(sorted((change_values or {}).items())),
        "skill_name": skill_name,
    }


SkillProposalBody.model_rebuild()
SkillDetail.model_rebuild()
PromotionPreview.model_rebuild()
ProposalDetail.model_rebuild()


__all__ = [
    "ALLOWED_CHANGE_PATHS",
    "CASE_ID_PATTERN",
    "CROSS_CHECKED_PATHS",
    "DECISION_CLASS_PATTERN",
    "DEFAULT_HOLDOUT_TREATMENT_FRACTION",
    "DEFERRED_SUITES",
    "EXPERIMENT_ACTIVATOR_SETTING",
    "EXPERIMENT_KEY_PATTERN",
    "EXPERIMENT_TRANSITIONS",
    "EXPERIMENT_UNIT_BY_KIND",
    "FROZEN_POLICY_PATHS",
    "HARD_FAIL_SUITES",
    "HUMAN_ONLY_STATUSES",
    "LEARNING_ACTOR",
    "LEARNING_EMITTED_BY",
    "LEARNING_PROMOTER_SETTING",
    "MAX_CASE_ID_CHARS",
    "MAX_CHANGES_PER_PROPOSAL",
    "MAX_DESCRIPTION_CHARS",
    "MAX_HOLDOUT_TREATMENT_FRACTION",
    "MAX_INSTRUCTIONS_CHARS",
    "MAX_NOTE_CHARS",
    "MAX_OBSERVATIONS",
    "MAX_OPTIONS_PER_PROPOSAL",
    "MAX_RATIONALE_CHARS",
    "MAX_SOURCE_REFS",
    "MAX_TITLE_CHARS",
    "MIN_SAMPLE_COUNT",
    "NO_RELATIONSHIP",
    "OPEN_PROPOSAL_STATUSES",
    "PATH_PATTERN",
    "PROMOTER_ACTOR_PATTERN",
    "PROPOSAL_TRANSITIONS",
    "PROPOSAL_TRANSITION_AUTHORITY",
    "PROPOSER_PATTERN",
    "REASONS_BY_PROPOSAL_STATUS",
    "SHA256_PATTERN",
    "SKILL_NAME_PATTERN",
    "SKILL_TRANSITIONS",
    "SUBJECT_LEARNING_PROPOSAL",
    "TERMINAL_PROPOSAL_STATUSES",
    "VERSION_PATTERN",
    "AdvanceProposalRequest",
    "AdvanceProposalResponse",
    "AnchorSource",
    "CaseOrigin",
    "ChangePolicy",
    "CorpusBanner",
    "CrossPolicyContext",
    "DatasetManifest",
    "DecisionClass",
    "DecisionClassMetrics",
    "DecisionClassObservation",
    "DescriptionText",
    "EvalCase",
    "EvalCaseInputs",
    "EvalExpectation",
    "EvalPrivacy",
    "EvalProvenance",
    "EvalSuite",
    "EvaluationDetail",
    "EvaluationMetrics",
    "EvaluationMode",
    "EvaluationNotFoundError",
    "EvaluationSummary",
    "EvidenceWindow",
    "ExperimentArm",
    "ExperimentAssignmentView",
    "ExperimentDetail",
    "ExperimentKind",
    "ExperimentNotFoundError",
    "ExperimentPolicy",
    "ExperimentRefusal",
    "ExperimentRefusedError",
    "ExperimentStatus",
    "ExperimentUnit",
    "ExperimentView",
    "GatePolicy",
    "GateReason",
    "GateReasonCode",
    "GateResult",
    "LearningError",
    "LearningOverview",
    "LearningPolicy",
    "LearningReader",
    "NoteText",
    "PolicyCandidateRef",
    "PolicyCandidateView",
    "PolicyChange",
    "PolicyPath",
    "PolicyProposalBody",
    "PromotionPreview",
    "PromotionRefusal",
    "PromotionRefusedError",
    "ProposalBody",
    "ProposalDetail",
    "ProposalDraft",
    "ProposalEvidence",
    "ProposalKind",
    "ProposalNotFoundError",
    "ProposalOption",
    "ProposalRejectionReason",
    "ProposalStatus",
    "ProposalSummary",
    "ProposalTransitionView",
    "RationaleText",
    "RecordedModelResponse",
    "SkillDetail",
    "SkillNotFoundError",
    "SkillProposalBody",
    "SkillSource",
    "SkillStatus",
    "SkillSummary",
    "SuiteGate",
    "SuiteMetrics",
    "TargetKind",
    "TitleText",
    "experiment_unit",
    "fingerprint_payload",
    "is_allowed_proposal_transition",
    "is_canonical_decision_class",
    "is_frozen_path",
    "is_learnable_path",
    "may_advance_proposal",
]
