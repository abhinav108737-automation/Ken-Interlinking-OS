"""Autonomy-first publishing contracts (ADR-0018).

FROZEN CONTRACT. Implements docs/adr/ADR-0018-autonomy-first-publishing.md, which
**supersedes the human-approval workflow ADR-0013 section 1 made the normal path** and
replaces the decision classes once planned as BLOCK / REQUIRE_APPROVAL /
AUTO_APPROVE_ELIGIBLE.

There is no normal human approval step. Publishing is governed by policy plus an autonomy
decision; humans monitor, administer policy, investigate blocks and rollbacks, handle
exceptions and override.

**THE RULE THAT MATTERS MOST.** Every :class:`BlockCode` is DETERMINISTIC: it is computed
from stored facts -- the inventory, the analyses, the protections, the plan, the CMS page and
the vector freshness -- by code, **without any model call**, and it is evaluated BEFORE
anything else. A model can never turn a block into a publish, and no decision, confidence,
risk tier or operator convenience may bypass one. Two guards keep this honest rather than
documented: :meth:`AutonomyEngine.deterministic_blocks` is specified to perform no inference,
and a static test asserts the block evaluator's module imports no
:class:`~ken_contracts.model.ModelProvider`.

What a model MAY do: lower confidence, raise a risk tier, add a critique, or decline to act
(:attr:`CriticOutcome.NO_ACTION`). What it may never do: clear a block, raise a decision, or
promote a policy (ADR-0016 section 15 keeps promotion a human control-plane action).

Modes (``KEN_AUTONOMY_MODE``, default ``shadow``) are in :class:`AutonomyMode`. Because the
default is ``shadow``, installing this product changes nothing on a real site until an
operator deliberately moves to ``guarded`` or ``autonomous`` AND acknowledges it
(``KEN_AUTONOMY_AUTONOMOUS_ACKNOWLEDGED``), with production writes gated again by
``KEN_AUTONOMY_ALLOW_PRODUCTION``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from ken_contracts.agents import CriticVerdict
from ken_contracts.common import ContractModel, FrozenModel
from ken_contracts.errors import ListResponse
from ken_contracts.recommendations import ACTOR_PATTERN, LinkPlanStatus

__all__ = [
    "AUTONOMOUS_CONFIDENCE_FLOOR_MIN",
    "COOLDOWN_FLOOR_S",
    "DETERMINISTIC_BLOCK_CODES",
    "FAILED_ATTEMPTS_FOR_HIGH_CEILING",
    "GUARDED_CONFIDENCE_FLOOR_MIN",
    "HUB_INBOUND_LINKS_CEILING",
    "MAX_OVERRIDE_REASON_CHARS",
    "POSITIVE_OUTCOME_RATE_FLOOR_MIN",
    "PUBLISH_ATTEMPTS_CEILING",
    "REDIRECT_HOPS_CEILING",
    "REQUIRED_COMMERCIAL_PAGE_TYPES",
    "SENSITIVE_ANCHOR_CHARS_CEILING",
    "SUBJECT_AUTONOMY_DECISION",
    "AutonomyDecision",
    "AutonomyDecisionView",
    "AutonomyEngine",
    "AutonomyError",
    "AutonomyEvaluation",
    "AutonomyHealth",
    "AutonomyMode",
    "AutonomyPolicy",
    "AutonomyReader",
    "AutonomySummary",
    "BlockCode",
    "BlockPolicy",
    "BlockedActionView",
    "ConfidencePolicy",
    "CriticOutcome",
    "OperatorOverrideRequest",
    "OperatorOverrideResponse",
    "OverrideAction",
    "OverrideRefusedError",
    "PlanNotEvaluatedError",
    "PublishAttemptView",
    "RetryBudgetState",
    "RetryPolicy",
    "RiskPolicy",
    "RiskTier",
    "VerificationProfile",
]

MAX_OVERRIDE_REASON_CHARS: Final = 500
SUBJECT_AUTONOMY_DECISION: Final = "autonomy_decision"


# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
class AutonomyDecision(StrEnum):
    """ADR-0018 section 1. Exactly three outcomes; there is no "approve" among them."""

    BLOCK = "block"
    """Never publish. Deterministic hard blocks and policy refusals land here."""
    AUTO_PUBLISH = "auto_publish"
    """Publish without a human, then verify as usual."""
    AUTO_PUBLISH_WITH_EXTRA_VERIFICATION = "auto_publish_with_extra_verification"
    """Publish, then run the STRICT verification profile. It differs from
    :attr:`AUTO_PUBLISH` only in the verification profile -- never in the safety rules."""


class AutonomyMode(StrEnum):
    """``KEN_AUTONOMY_MODE``. Default :attr:`SHADOW`, deliberately."""

    SHADOW = "shadow"
    """Evaluate and RECORD the decision that would have been taken. Publish nothing."""
    GUARDED = "guarded"
    """Auto-publish only low-risk, high-confidence :attr:`AutonomyDecision.AUTO_PUBLISH`
    decisions; defer the rest to an operator or block them."""
    AUTONOMOUS = "autonomous"
    """Publish every policy-eligible decision. Hard safety rules still apply, unchanged."""


class RiskTier(StrEnum):
    """How much a mistake on this pair would cost. Deterministic inputs only (page type,
    commercial importance, inbound traffic, protection proximity, anchor sensitivity)."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CriticOutcome(StrEnum):
    """The autonomy-level view of the critic step.

    :attr:`NO_ACTION` is a SEPARATE, EARLIER outcome (ADR-0018 section 1): the plan is not a
    defect, there is simply nothing worth doing. It never becomes a publish.

    Relationship to :class:`~ken_contracts.agents.CriticVerdict`, which is the model-boundary
    vocabulary: ``accept``/``reject`` map across directly; ``uncertain`` does NOT map to
    :attr:`ACCEPT` silently -- it is recorded in
    :attr:`AutonomyEvaluation.critic_verdict` and raises the risk tier, which in
    ``guarded`` mode defers the plan instead of publishing it.
    """

    ACCEPT = "accept"
    REJECT = "reject"
    NO_ACTION = "no_action"


class VerificationProfile(StrEnum):
    """Which verification runs after a publish (ADR-0014 verification, unchanged in kind)."""

    STANDARD = "standard"
    """One HTML/HTTP verification pass, as Phase 6 defines it."""
    STRICT = "strict"
    """HTTP re-fetch PLUS a second pass after a delay, and a stricter mutation-isolation
    check. Selected by :attr:`AutonomyDecision.AUTO_PUBLISH_WITH_EXTRA_VERIFICATION`."""


class BlockCode(StrEnum):
    """The CLOSED set of deterministic hard blocks (the completion directive section 9).

    Every one of these is computable from stored facts by code alone -- no model call, no
    judgement. A block is terminal for the plan unless an operator overrides it, and the
    override is recorded with who and why (:class:`OperatorOverrideRequest`).
    """

    SELF_LINK = "self_link"
    """Source and destination are the same page."""
    EXTERNAL_DESTINATION = "external_destination"
    """The destination is not an in-scope internal page."""
    PROTECTED_PAGE = "protected_page"
    """An active protection covers the source or the destination."""
    UNPUBLISHED_DESTINATION = "unpublished_destination"
    """The destination is draft, scheduled, private or otherwise not published."""
    BROKEN_TARGET = "broken_target"
    """The destination does not resolve (404/410/5xx, or gone from the inventory)."""
    STALE_SOURCE_VERSION = "stale_source_version"
    """The plan was built against a source version that is no longer current and cannot be
    rebased (``PlanFreshness.STALE``)."""
    DUPLICATE_CONTEXTUAL_LINK = "duplicate_contextual_link"
    """A contextual link to this destination already exists on the source and the policy
    does not allow a duplicate."""
    PROHIBITED_PAGE_TYPE = "prohibited_page_type"
    """The source or destination page type may not take part in contextual linking."""
    POLICY_DISALLOWED_DESTINATION = "policy_disallowed_destination"
    """The destination is excluded by policy (excluded paths, incompatible page types,
    cool-down after a rejection)."""
    INVALID_CANONICAL = "invalid_canonical"
    """The destination is a canonical alias, or its canonical URL points elsewhere."""
    UNSUPPORTED_REDIRECT = "unsupported_redirect"
    """The destination redirects in a way the system will not publish behind (chain, loop,
    cross-host, or a redirect to a non-canonical URL)."""
    INVALID_PAGE_STATE = "invalid_page_state"
    """The page state forbids the change: noindex, thin, gone, or a CMS lock."""
    INVALID_ANCHOR = "invalid_anchor"
    """The anchor is not a verbatim, placement-safe span of the current block text (hidden
    ancestor, forbidden ancestor, fabricated or moved anchor, unsafe href)."""
    MISSING_ROLLBACK_METADATA = "missing_rollback_metadata"
    """The exact information an inverse splice needs is absent, so the change would not be
    reversible. Nothing may be published that cannot be rolled back."""
    STALE_VECTOR = "stale_vector"
    """The retrieval that produced this plan rests on a superseded vector (ADR-0017 s4): a
    stale hit may never place a link."""


DETERMINISTIC_BLOCK_CODES: Final[frozenset[BlockCode]] = frozenset(BlockCode)
"""EVERY block code is deterministic. The set is named so a test can assert the two are
equal: if a non-deterministic "block" is ever added, that test fails and the design decision
has to be made explicitly rather than by accident."""


# --------------------------------------------------------------------------- #
# The evaluation record
# --------------------------------------------------------------------------- #
class AutonomyEvaluation(FrozenModel):
    """One evaluation of one plan: what was decided, under which mode, and on what evidence.

    Persisted append-only as an ``autonomy_decisions`` row (ADR-0018 section 6). This is what
    Phase 9 calibration and Phase 8 learning read.
    """

    plan_id: UUID
    decision: AutonomyDecision
    mode: AutonomyMode = Field(description="The mode in force when this was evaluated.")
    risk_tier: RiskTier
    confidence: float = Field(
        ge=0.0, le=1.0, description="The confidence the decision was taken on."
    )
    calibration_version: str | None = Field(
        default=None,
        description=(
            "None = RAW confidence (ADR-0011: no threshold may treat raw as calibrated). A "
            "guarded auto-publish therefore requires a calibration version, which is what "
            "keeps Phase 9's calibration on the critical path rather than optional."
        ),
    )
    critic_outcome: CriticOutcome
    critic_verdict: CriticVerdict | None = Field(
        default=None,
        description="The raw model-boundary verdict, kept so `uncertain` is not lost.",
    )
    policy_version: int = Field(
        ge=1, description="Version of the governing policy document this was evaluated under."
    )
    blocks: tuple[BlockCode, ...] = Field(
        default=(),
        description="The deterministic blocks that FIRED, in enum order. Non-empty implies "
        "``decision == BLOCK``.",
    )
    rollback_available: bool = Field(
        description=(
            "Whether an inverse splice is possible for this plan right now. False forces "
            "``BLOCK`` with ``MISSING_ROLLBACK_METADATA``: nothing is published that cannot "
            "be undone."
        )
    )
    verification_profile: VerificationProfile = VerificationProfile.STANDARD
    would_have_decision: AutonomyDecision | None = Field(
        default=None,
        description=(
            "SHADOW ONLY: the decision that WOULD have been taken had the mode been "
            "``autonomous``. Set exactly when ``mode == shadow``; in shadow the effective "
            "``decision`` is never a publish."
        ),
    )
    evaluated_at: AwareDatetime
    engine_version: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _consistent(self) -> AutonomyEvaluation:
        if self.blocks and self.decision is not AutonomyDecision.BLOCK:
            raise ValueError("a fired block code forces decision=block")
        if len(set(self.blocks)) != len(self.blocks):
            raise ValueError("block codes must be unique")
        if tuple(sorted(self.blocks, key=_block_order)) != self.blocks:
            raise ValueError("block codes must be in enum order")
        if not self.rollback_available and self.decision is not AutonomyDecision.BLOCK:
            raise ValueError("a plan without rollback availability can only be blocked")
        if self.decision is AutonomyDecision.BLOCK and self.critic_outcome is (
            CriticOutcome.NO_ACTION
        ):
            # no_action is an EARLIER outcome: there is nothing to block.
            raise ValueError("critic_outcome=no_action cannot be a block decision")
        if (self.mode is AutonomyMode.SHADOW) != (self.would_have_decision is not None):
            raise ValueError("would_have_decision is set exactly in shadow mode")
        if self.mode is AutonomyMode.SHADOW and self.decision is not AutonomyDecision.BLOCK:
            # Shadow publishes nothing; the counterfactual carries what it would have done.
            raise ValueError("in shadow mode the effective decision is never a publish")
        strict = self.decision is AutonomyDecision.AUTO_PUBLISH_WITH_EXTRA_VERIFICATION
        if strict and self.verification_profile is not VerificationProfile.STRICT:
            raise ValueError("auto_publish_with_extra_verification requires the strict profile")
        return self

    @property
    def deferred(self) -> bool:
        """A DEFERRAL: ``BLOCK`` with no fired code, taken outside shadow, not by a critic's
        rejection. Explicit rather than an unwritten convention.

        There is no separate "deferred" state in the operational vocabulary (proposed,
        blocked, auto-published, verification-pending, verified, rolled-back, failed): a
        deferral is a BLOCKED plan whose block list is empty because no deterministic rule
        fired -- the mode, risk tier, confidence or calibration declined to publish it
        unattended. An operator can still publish-override it. Shadow rows are excluded
        (every shadow decision is ``BLOCK`` by definition and nothing was deferred), as is a
        critic rejection (a refusal, not a postponement).
        """
        return (
            self.decision is AutonomyDecision.BLOCK
            and not self.blocks
            and self.mode is not AutonomyMode.SHADOW
            and self.critic_outcome is not CriticOutcome.REJECT
        )


def _block_order(code: BlockCode) -> int:
    return list(BlockCode).index(code)


class RetryBudgetState(FrozenModel):
    """The per-(source, target) retry budget and cool-down (ADR-0018 section 4).

    A failed publish is recorded, the pair is marked, and the SAME plan is never retried in a
    loop: ``attempts >= max_attempts`` or ``cooldown_until`` in the future means no publish,
    whatever the decision says. Deterministic, stored, and checked under the plan's lock.
    """

    source_page_id: UUID
    target_page_id: UUID
    attempts: int = Field(ge=0)
    max_attempts: int = Field(ge=1, le=10)
    failures: int = Field(ge=0)
    cooldown_until: AwareDatetime | None = None
    last_attempt_at: AwareDatetime | None = None
    exhausted: bool = Field(description="``attempts >= max_attempts``: no further publish.")


class PublishAttemptView(FrozenModel):
    """One recorded publish attempt for a (source, target) pair. Append-only."""

    id: int
    plan_id: UUID
    source_page_id: UUID
    target_page_id: UUID
    attempt: int = Field(ge=1)
    decision: AutonomyDecision
    mode: AutonomyMode
    succeeded: bool
    verification_profile: VerificationProfile
    failure_code: str | None = Field(default=None, max_length=64)
    rolled_back: bool = False
    cooldown_until: AwareDatetime | None = None
    occurred_at: AwareDatetime


# --------------------------------------------------------------------------- #
# API read models
# --------------------------------------------------------------------------- #
class AutonomyDecisionView(AutonomyEvaluation):
    """A stored evaluation, with its row id and the plan's current status."""

    id: int
    plan_status: LinkPlanStatus
    source_url: str
    target_url: str


class BlockedActionView(FrozenModel):
    """A blocked plan, for the operator screen that replaces the approval inbox."""

    plan_id: UUID
    source_page_id: UUID
    source_url: str
    target_page_id: UUID
    target_url: str
    anchor_text: str
    blocks: tuple[BlockCode, ...]
    risk_tier: RiskTier
    mode: AutonomyMode
    decided_at: AwareDatetime
    overridden: bool = Field(
        default=False, description="An operator has already acted on this block."
    )
    reason: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "Why the plan is blocked, in words, as recorded when it was blocked: either the "
            "deterministic codes that fired, or -- for a DEFERRAL, whose block list is empty -- "
            "'deferred' with the risk tier, confidence and calibration facts that declined it."
        ),
    )


class AutonomyHealth(FrozenModel):
    """``GET /autonomy/mode``: the mode in force and why it is safe, in one payload."""

    mode: AutonomyMode
    autonomous_acknowledged: bool
    allow_production: bool
    cms_provider: str
    cms_writes_enabled: bool
    publishing_enabled: bool = Field(
        description="True only when the mode, the acknowledgement and the CMS all permit a "
        "real write. False in shadow, always."
    )
    verification_profile_default: VerificationProfile
    calibration_version: str | None = None
    policy_version: int = Field(ge=1)


class AutonomySummary(FrozenModel):
    decisions_by_decision: dict[str, int]
    decisions_by_mode: dict[str, int]
    blocks_by_code: dict[str, int]
    blocked_plans: int = Field(ge=0)
    auto_published: int = Field(ge=0)
    rolled_back: int = Field(ge=0)
    mode: AutonomyMode


# --------------------------------------------------------------------------- #
# Operator override (the exception path)
# --------------------------------------------------------------------------- #
class OverrideAction(StrEnum):
    """ADR-0018 section 5: an operator may publish or block ONE plan, with a reason."""

    PUBLISH = "publish"
    """Publish a plan the autonomy engine did not publish. It does NOT clear a deterministic
    block: an override of a blocked plan is refused unless the block has cleared."""
    BLOCK = "block"
    """Block a plan the autonomy engine would publish."""


class OperatorOverrideRequest(ContractModel):
    operation_id: UUID = Field(description="Idempotency key; a replay returns the stored result.")
    action: OverrideAction
    reason: str = Field(min_length=1, max_length=MAX_OVERRIDE_REASON_CHARS)
    expected_status: LinkPlanStatus = Field(
        description="The plan's status the caller saw. A mismatch is refused (version-aware)."
    )


class OperatorOverrideResponse(FrozenModel):
    plan_id: UUID
    action: OverrideAction
    applied: bool
    plan_status: LinkPlanStatus
    actor: str = Field(pattern=ACTOR_PATTERN)
    blocks: tuple[BlockCode, ...] = Field(
        default=(),
        description="Blocks still firing. Non-empty with ``action=publish`` means the "
        "override was REFUSED: a deterministic block is never overridable.",
    )
    refusal: str | None = Field(default=None, max_length=200)
    occurred_at: AwareDatetime


# --------------------------------------------------------------------------- #
# The autonomy policy (policies/autonomy/autonomy.v<N>.yaml)
# --------------------------------------------------------------------------- #
# DATA, not code: every threshold that decides whether the system may publish without a human
# lives in a versioned document, so a change is a reviewed file and a new version -- never an
# in-place edit, and never a number only a code change can alter. The directive makes autonomy
# *policy-governed*; this is that policy.
#
# THE ONE RULE THAT MAKES A HUMAN-EDITABLE SAFETY DOCUMENT SAFE: it may only make the system
# MORE conservative than the floor below, never less. Each ``*_MIN`` / ``*_CEILING`` constant
# is the least cautious value a policy file may hold; a file beyond it is a ValidationError at
# load (the ``LearningPolicy`` pattern, ADR-0016 15.2c). The floors are deliberately not
# chosen to make any particular corpus publish: if ``guarded`` publishes nothing on a given
# corpus, that is the honest outcome.
GUARDED_CONFIDENCE_FLOOR_MIN: Final = 0.75
"""``guarded`` publishes only HIGH-confidence actions. Below three-in-four, 'high' is a
stretch of the word."""
AUTONOMOUS_CONFIDENCE_FLOOR_MIN: Final = 0.50
"""Even ``autonomous`` refuses a plan the engine itself believes less than a coin flip."""
POSITIVE_OUTCOME_RATE_FLOOR_MIN: Final = 0.50
"""A decision class whose measured outcomes are mostly non-positive has its confidence
capped. BEHAVIOURAL, so one-directional: a high rate never raises anything (ADR-0015 s12)."""
PUBLISH_ATTEMPTS_CEILING: Final = 5
"""No pair gets more than five publish attempts, whatever a file says."""
COOLDOWN_FLOOR_S: Final = 600.0
"""A failed pair always waits at least ten minutes before it is touched again."""
HUB_INBOUND_LINKS_CEILING: Final = 50
SENSITIVE_ANCHOR_CHARS_CEILING: Final = 80
FAILED_ATTEMPTS_FOR_HIGH_CEILING: Final = 2
REDIRECT_HOPS_CEILING: Final = 1
REQUIRED_COMMERCIAL_PAGE_TYPES: Final[frozenset[str]] = frozenset(
    {"product", "pricing", "service", "offer", "checkout", "contact", "lead_form"}
)
"""Page types whose purpose is a transaction. A file may ADD types and may not remove one: a
mistake on a commercial page costs revenue, so it is never ``low`` risk."""


class ConfidencePolicy(FrozenModel):
    guarded_min_confidence: float = Field(
        ge=GUARDED_CONFIDENCE_FLOOR_MIN,
        le=1.0,
        description="``guarded`` mode defers any plan whose calibrated confidence is below this.",
    )
    autonomous_min_confidence: float = Field(
        ge=AUTONOMOUS_CONFIDENCE_FLOOR_MIN,
        le=1.0,
        description="``autonomous`` mode still refuses a plan below this.",
    )
    min_positive_outcome_rate: float = Field(
        ge=POSITIVE_OUTCOME_RATE_FLOOR_MIN,
        le=1.0,
        description="A class whose measured positive-outcome rate is below this has its "
        "confidence capped at that rate. Behavioural: it can lower a confidence, never raise.",
    )

    @model_validator(mode="after")
    def _ordered(self) -> ConfidencePolicy:
        if self.guarded_min_confidence < self.autonomous_min_confidence:
            raise ValueError(
                "guarded_min_confidence must be at least autonomous_min_confidence: "
                "guarded is the stricter mode"
            )
        return self


class RetryPolicy(FrozenModel):
    max_attempts: int = Field(
        ge=1,
        le=PUBLISH_ATTEMPTS_CEILING,
        description="Publish attempts per (source, target) pair, ever.",
    )
    cooldown_s: float = Field(
        ge=COOLDOWN_FLOOR_S,
        le=30 * 86400.0,
        description="The wait after a failed publish or an automatic rollback.",
    )


class RiskPolicy(FrozenModel):
    hub_inbound_links: int = Field(
        ge=1,
        le=HUB_INBOUND_LINKS_CEILING,
        description="A destination with at least this many inbound internal links is a hub.",
    )
    sensitive_anchor_chars: int = Field(
        ge=10,
        le=SENSITIVE_ANCHOR_CHARS_CEILING,
        description="An anchor longer than this spans too much of the sentence.",
    )
    failed_attempts_for_high: int = Field(
        ge=1,
        le=FAILED_ATTEMPTS_FOR_HIGH_CEILING,
        description="This many FAILED publishes of a pair make it high risk.",
    )
    commercial_page_types: tuple[str, ...] = Field(
        min_length=1,
        description="Page types that are never low risk. May add to the required set, not "
        "remove from it.",
    )

    @model_validator(mode="after")
    def _superset(self) -> RiskPolicy:
        if len(set(self.commercial_page_types)) != len(self.commercial_page_types):
            raise ValueError("commercial_page_types must be unique")
        missing = REQUIRED_COMMERCIAL_PAGE_TYPES - set(self.commercial_page_types)
        if missing:
            raise ValueError(
                "commercial_page_types cannot drop a required type; missing "
                + ", ".join(sorted(missing))
            )
        return self


class BlockPolicy(FrozenModel):
    max_redirect_hops: int = Field(
        ge=0,
        le=REDIRECT_HOPS_CEILING,
        description="A destination behind more redirect hops than this is UNSUPPORTED_REDIRECT.",
    )


class AutonomyPolicy(FrozenModel):
    """``policies/autonomy/autonomy.v<N>.yaml``. Validated on load; served read-only by
    ``/settings/policies``; its ``version`` is recorded on every ``autonomy_decisions`` row."""

    policy: Literal["autonomy"]
    version: int = Field(ge=1)
    status: str = Field(min_length=1, max_length=64)
    source: str = Field(min_length=1, max_length=500)
    confidence: ConfidencePolicy
    retry: RetryPolicy
    risk: RiskPolicy
    blocks: BlockPolicy


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class AutonomyError(Exception):
    """Base class for autonomy failures."""


class PlanNotEvaluatedError(AutonomyError):
    """No autonomy decision exists for this plan."""


class OverrideRefusedError(AutonomyError):
    """The override cannot be applied (a block still fires, or the status moved on)."""


# --------------------------------------------------------------------------- #
# Protocols
# --------------------------------------------------------------------------- #
@runtime_checkable
class AutonomyEngine(Protocol):
    """Decides, records and never publishes by itself.

    :meth:`deterministic_blocks` **performs no inference.** It reads stored facts and returns
    codes; it must be callable with the model server switched off and return the same answer.
    :meth:`evaluate` calls it FIRST and short-circuits to :attr:`AutonomyDecision.BLOCK` on
    any code, before confidence, risk or a critic is consulted.
    """

    async def deterministic_blocks(self, *, plan_id: UUID) -> tuple[BlockCode, ...]: ...

    async def evaluate(
        self, *, plan_id: UUID, mode: AutonomyMode | None = None
    ) -> AutonomyEvaluation: ...

    async def retry_budget(
        self, *, source_page_id: UUID, target_page_id: UUID
    ) -> RetryBudgetState: ...


@runtime_checkable
class AutonomyReader(Protocol):
    """The API's only route to stored autonomy data (READ ONLY sessions)."""

    async def get_health(self) -> AutonomyHealth: ...

    async def get_summary(self) -> AutonomySummary: ...

    async def list_decisions(
        self,
        *,
        limit: int,
        offset: int,
        decision: AutonomyDecision | None = None,
        mode: AutonomyMode | None = None,
        plan_id: UUID | None = None,
    ) -> ListResponse[AutonomyDecisionView]: ...

    async def list_blocked(
        self, *, limit: int, offset: int, code: BlockCode | None = None
    ) -> ListResponse[BlockedActionView]: ...

    async def list_attempts(
        self, *, limit: int, offset: int, plan_id: UUID | None = None
    ) -> ListResponse[PublishAttemptView]: ...
