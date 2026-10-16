"""Autonomy service interfaces (docs/adr/ADR-0018-autonomy-first-publishing.md).

FROZEN (orchestrator). The autonomy workstream implements these; it does not change a
signature. A few tiny PURE helpers are implemented HERE
(:func:`decision_for`, :func:`profile_for`, :func:`budget_from_attempts`) because several
workstreams -- the engine, the deployment service, the API reader and the evals -- must
compute them IDENTICALLY, and a second copy is how two of them start disagreeing.

Rules (binding):

* **The deterministic blocks come first and come from code.**
  :class:`DeterministicBlockEvaluator` is the ONLY producer of
  :class:`~ken_contracts.autonomy.BlockCode`, it takes no
  :class:`~ken_contracts.model.ModelProvider`, and it must answer the same way with the model
  server switched off. :meth:`AutonomyEngine.evaluate` calls it FIRST and short-circuits to
  ``BLOCK``; no confidence, risk tier, critic opinion or operator convenience may clear a
  code. A static test asserts the evaluator's module imports no model provider.
* **A model may lower, never raise.** The critic and the calibrator can reduce confidence,
  raise a risk tier or decline to act; nothing a model returns can produce a publish that the
  deterministic layer did not already permit.
* **Raw confidence is not calibrated confidence.** :class:`CalibrationReader` reports the
  calibration version alongside the value, and ``guarded`` mode must refuse to auto-publish
  on a ``None`` version (ADR-0011: no threshold may treat raw as calibrated).
* **Nothing is published that cannot be rolled back.** ``rollback_available`` is computed from
  the stored deployment metadata, and a false value forces ``BLOCK`` with
  ``MISSING_ROLLBACK_METADATA`` -- which is also a database CHECK, so the record cannot claim
  otherwise.
* **The retry budget is checked under the plan's lock**, immediately before the publish, not
  when the decision was taken: a decision can be minutes old, and the pair may have failed
  since. :class:`RetryBudgetGate` is the only reader of it.
* **Shadow mode writes exactly one thing**: an ``autonomy_decisions`` row carrying the
  counterfactual. A test asserts a shadow pass writes no ``deployments``, no
  ``publish_attempts`` and no plan transition.
* **An override is a human act, recorded as one.** :class:`OperatorOverrideService` takes a
  :class:`~ken_contracts.auth.Principal` -- never an actor string -- because the database can
  only check that an actor LOOKS like ``user:<name>`` (the ADR-0016 15.7.3 rule, same
  reasoning). It re-evaluates the deterministic blocks and REFUSES a publish override while
  any code still fires.
* **Every pass is a ``job_runs`` row** of kind ``autonomy`` (or ``autonomy_shadow`` in shadow
  mode) and emits ``job.completed``, with a per-kind advisory lock.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Protocol

from ken_contracts.auth import Principal
from ken_contracts.autonomy import (
    AutonomyDecision,
    AutonomyEvaluation,
    AutonomyMode,
    BlockCode,
    OperatorOverrideRequest,
    OperatorOverrideResponse,
    RetryBudgetState,
    RiskTier,
    VerificationProfile,
)

__all__ = [
    "AUTONOMY_ACTOR",
    "ENGINE_VERSION",
    "AutonomyEngine",
    "CalibratedConfidence",
    "CalibrationReader",
    "DeterministicBlockEvaluator",
    "OperatorOverrideService",
    "PlanFacts",
    "RetryBudgetGate",
    "RiskClassifier",
    "VerificationProfileSelector",
    "budget_from_attempts",
    "decision_for",
    "profile_for",
]

ENGINE_VERSION: Final = "autonomy-engine-1"
"""Recorded on every ``autonomy_decisions`` row, so a decision is attributable to a build."""

AUTONOMY_ACTOR: Final = "system:autonomy-engine"
"""The ONLY actor the autonomous path writes. An override writes ``user:<username>``, bound
inside :class:`OperatorOverrideService` from the session and never from a request body."""


# --------------------------------------------------------------------------- #
# Deterministic inputs
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class PlanFacts:
    """The stored facts the deterministic layer decides on. NO model output, by construction.

    Assembled by one SQL read so a decision is taken on ONE snapshot: computing these
    piecemeal is how a plan gets blocked for a stale version it no longer has.
    """

    plan_id: uuid.UUID
    source_page_id: uuid.UUID
    target_page_id: uuid.UUID
    source_page_version: int
    source_current_version: int
    target_page_version: int
    target_current_version: int
    source_protected: bool
    target_protected: bool
    target_published: bool
    target_gone: bool
    target_http_status: int | None
    target_canonical_ok: bool
    target_redirect_supported: bool
    target_noindex: bool
    target_thin: bool
    source_page_type: str | None
    target_page_type: str | None
    page_types_compatible: bool
    destination_internal: bool
    destination_allowed_by_policy: bool
    already_linked: bool
    duplicate_allowed: bool
    anchor_valid: bool
    """The anchor is still a verbatim, placement-safe span of the CURRENT block text."""
    rollback_metadata_present: bool
    vector_fresh: bool
    """The retrieval behind this plan rested on a current vector (ADR-0017 section 4)."""
    policy_version: int
    confidence: float
    calibration_version: str | None


# --------------------------------------------------------------------------- #
# Pure helpers (shared on purpose)
# --------------------------------------------------------------------------- #
def decision_for(
    *,
    blocks: Sequence[BlockCode],
    mode: AutonomyMode,
    risk_tier: RiskTier,
    extra_verification: bool,
) -> AutonomyDecision:
    """The decision these inputs imply. The ONE place the mapping lives.

    Blocks win, always and first. In ``shadow`` the effective decision is always ``BLOCK``
    (the counterfactual is recorded separately), because shadow publishes nothing.
    """
    if blocks:
        return AutonomyDecision.BLOCK
    if mode is AutonomyMode.SHADOW:
        return AutonomyDecision.BLOCK
    if mode is AutonomyMode.GUARDED and risk_tier is not RiskTier.LOW:
        # Guarded auto-publishes only LOW risk; the rest is deferred by the caller to the
        # operator queue, which is a status move, not a different decision.
        return AutonomyDecision.BLOCK
    if extra_verification or risk_tier is RiskTier.HIGH:
        return AutonomyDecision.AUTO_PUBLISH_WITH_EXTRA_VERIFICATION
    return AutonomyDecision.AUTO_PUBLISH


def profile_for(decision: AutonomyDecision, *, default: VerificationProfile) -> VerificationProfile:
    """The verification profile a decision demands. ``STRICT`` is never downgraded."""
    if decision is AutonomyDecision.AUTO_PUBLISH_WITH_EXTRA_VERIFICATION:
        return VerificationProfile.STRICT
    return default


def budget_from_attempts(
    *,
    source_page_id: uuid.UUID,
    target_page_id: uuid.UUID,
    attempts: int,
    failures: int,
    max_attempts: int,
    cooldown_until: datetime | None,
    last_attempt_at: datetime | None,
) -> RetryBudgetState:
    """Assemble the budget state. ``exhausted`` is a function of the counts, not a flag that
    something has to remember to set."""
    return RetryBudgetState(
        source_page_id=source_page_id,
        target_page_id=target_page_id,
        attempts=attempts,
        max_attempts=max_attempts,
        failures=failures,
        cooldown_until=cooldown_until,
        last_attempt_at=last_attempt_at,
        exhausted=attempts >= max_attempts,
    )


@dataclass(frozen=True, slots=True)
class CalibratedConfidence:
    """A confidence AND the version that calibrated it. Never one without the other."""

    value: float
    calibration_version: str | None
    """``None`` = RAW. A guarded auto-publish must refuse on ``None``."""

    @property
    def is_calibrated(self) -> bool:
        return self.calibration_version is not None


@dataclass(frozen=True, slots=True)
class BudgetVerdict:
    """Why a publish may or may not proceed right now."""

    allowed: bool
    state: RetryBudgetState
    reasons: tuple[str, ...] = field(default=())
    """``budget_exhausted`` | ``cooling_down``. Stable codes, never prose."""


# --------------------------------------------------------------------------- #
# Protocols
# --------------------------------------------------------------------------- #
class DeterministicBlockEvaluator(Protocol):
    """The closed set of hard blocks, computed from stored facts. NO MODEL CALL.

    Implementations must be total: every input yields codes, never an exception, so a
    malformed plan is blocked rather than crashing a publish run.
    """

    async def facts(self, *, plan_id: uuid.UUID) -> PlanFacts:
        """One snapshot read. Raises only if the plan does not exist."""
        ...

    def evaluate(self, facts: PlanFacts) -> tuple[BlockCode, ...]:
        """Pure, synchronous and in :class:`BlockCode` enum order. Synchronous on purpose:
        a function with no I/O cannot smuggle in an inference call."""
        ...


class RiskClassifier(Protocol):
    """Deterministic risk tiering from stored facts (page type, commercial importance,
    inbound traffic, protection proximity, anchor sensitivity). No model call either: a tier
    gates publishing in ``guarded`` mode, so it must be reproducible and auditable."""

    async def classify(self, *, facts: PlanFacts) -> RiskTier: ...


class CalibrationReader(Protocol):
    """Reads the active calibration for a plan's decision class (Phase 9 calibration).

    Returns the RAW confidence with ``calibration_version=None`` when no calibration applies;
    it never invents one, because a fabricated version would let a guarded publish through.
    """

    async def calibrate(self, *, facts: PlanFacts) -> CalibratedConfidence: ...


class VerificationProfileSelector(Protocol):
    """Which profile to run after a publish. Wraps :func:`profile_for` with the policy and
    settings defaults, so the STRICT profile can be demanded but never waived."""

    async def select(
        self, *, facts: PlanFacts, decision: AutonomyDecision, risk_tier: RiskTier
    ) -> VerificationProfile: ...


class RetryBudgetGate(Protocol):
    """ADR-0018 section 4. The per-(source, target) budget and cool-down.

    :meth:`check` is called UNDER THE PLAN'S LOCK immediately before the publish, and
    :meth:`record` appends the attempt in the SAME transaction as the deployment intent, so a
    crash between them cannot lose an attempt and reopen the budget.
    """

    async def check(
        self, *, source_page_id: uuid.UUID, target_page_id: uuid.UUID
    ) -> BudgetVerdict: ...

    async def record(
        self,
        *,
        plan_id: uuid.UUID,
        source_page_id: uuid.UUID,
        target_page_id: uuid.UUID,
        decision: AutonomyDecision,
        mode: AutonomyMode,
        profile: VerificationProfile,
        succeeded: bool,
        failure_code: str | None,
        rolled_back: bool,
        actor: str,
        deployment_id: uuid.UUID | None = None,
    ) -> RetryBudgetState: ...


class AutonomyEngine(Protocol):
    """Evaluates and RECORDS; it never writes to the CMS itself.

    :meth:`evaluate_and_record` writes the ``autonomy_decisions`` row and, outside shadow
    mode, moves the plan to ``autonomy_decided`` or ``blocked`` in the SAME transaction --
    which migration 0010's deferred guard requires.
    """

    async def evaluate(
        self, *, plan_id: uuid.UUID, mode: AutonomyMode | None = None
    ) -> AutonomyEvaluation: ...

    async def evaluate_and_record(
        self, *, plan_id: uuid.UUID, mode: AutonomyMode | None = None
    ) -> AutonomyEvaluation: ...

    async def evaluate_source(
        self, *, source_page_id: uuid.UUID, mode: AutonomyMode | None = None
    ) -> list[AutonomyEvaluation]:
        """Every eligible plan of one source, serialised per source like the review service."""
        ...


class OperatorOverrideService(Protocol):
    """The exception path (ADR-0018 section 5). Takes a ``Principal``, never an actor string.

    A publish override is REFUSED while any deterministic block still fires: an override
    exists to act on a plan the engine declined to publish, not to waive a safety rule.
    """

    async def override(
        self,
        *,
        principal: Principal,
        plan_id: uuid.UUID,
        request: OperatorOverrideRequest,
    ) -> OperatorOverrideResponse: ...
