"""Phase 8 learning service interfaces (docs/adr/ADR-0016-learning.md, section 15 binding).

FROZEN (orchestrator). Reflection and proposals (W1), the eval runner and datasets (W2),
lifecycle, shadow, promotion and experiments (W3) and the API bodies (W4) implement these;
none of them changes a signature. A few tiny PURE helpers are implemented HERE
(:func:`code_only_violations`, :func:`experiment_arm`, :class:`BlockOnly`) because several
workstreams must compute them identically.

Rules (binding):

* **The promotion service takes a** :class:`~ken_contracts.auth.Principal`, **never an actor
  string** (section 15.7.3). The database can only check that an actor LOOKS like
  ``user:<name>``; only the session proves it is one. Two static rules keep this honest and a
  test enforces them: no module under ``ken_services/learning/`` contains a ``"user:"``
  literal, and only ``promote.py`` writes ``learning_proposal_transitions``.
  ADR-0016 15.7.3 calls the parameter an "AuthenticatedPrincipal"; the frozen contract type of
  an authenticated caller is ``ken_contracts.auth.Principal`` (ADR-0013), so that is the type
  used -- the requirement is the same one.
* **Promotion is refused while demonstration mode is on.** ``advance`` checks
  ``Settings.learning_demonstration_mode`` FIRST and raises
  :attr:`~ken_contracts.learning.PromotionRefusal.DEMONSTRATION_MODE`, before any other
  check, so Phase 8 cannot promote even with a passing run and an ``operator_admin`` session
  (ADR-0016 16.1).
* **The promoter guard.** ``promote.py`` runs
  ``SELECT set_config('ken.learning_promoter', 'on', true)`` in the SAME transaction as the
  transition INSERT and the proposal UPDATE. It is transaction-local, so it never leaks to the
  next transaction on a pooled connection. Nothing else in the codebase may set it.
* **Reflection's prompt carries CODE ONLY** (section 15.7.6): enum codes, integers and floats.
  No review ``note``, no anchor string, no page title, no URL. Anchor text IS page content
  (``recommendations.py``: an ``edited`` anchor is "copied verbatim from the block text") and
  reflection is corpus-level, so ONE poisoned anchor would steer EVERY proposal -- a strictly
  worse blast radius than the per-page agent prompt. :func:`code_only_violations` is the
  whitelist the rendered payload is tested against. If free text is ever genuinely needed,
  reuse the ``DATA`` + opaque-ref envelope from ``agents/ken_agents/prompts.py`` instead of
  loosening this.
* **The model only CHOOSES.** Options are enumerated by deterministic code and persisted in
  ``ProposalEvidence.options``; the model returns a
  :class:`~ken_contracts.learning.ProposalDraft` whose ``choice_index`` is validated against
  the live option count. Model disabled, unavailable or invalid -> option 0 plus
  ``fallback_used`` and a ``FallbackReason``, exactly as the agent nodes do.
* **Evidence is assembled only by ``aggregate.py``** -- every number in SQL/Python over the
  three streams. A static test asserts no other module constructs ``ProposalEvidence``.
* **Behavioural signals pass through** :class:`BlockOnly` (section 15.7.12 / 16.7). It can
  return blocking reasons and has no method that can pass a gate, so a spoofable first-party
  count can never graduate anything. Search data never gates at all.
* **Shadow writes nothing.** A shadow pass re-runs selection with the candidate inside the
  same job and records one ``policy_evaluations`` row; a test asserts zero ``link_plans`` rows
  are written around it.
* **Policy promotion is a CLI-only file write, and for ``ranking`` it is TWO files, atomically**
  (section 15.7.8): ``policies/ranking/ranking_weights.v<N+1>.yaml`` AND the ``ranking_policy:``
  pointer in a new recommendation document, because the ACTIVE ranking policy is resolved from
  that pointer by ``resolve_ranking_policy`` and NOT from ``Settings``. The writer must refuse
  unless a ``promoted`` transition with a ``^user:`` actor and an ``evaluation_run_id`` already
  exists, and must never create one (section 15.7.9).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.auth import Principal
from ken_contracts.learning import (
    MIN_SAMPLE_COUNT,
    AdvanceProposalRequest,
    AdvanceProposalResponse,
    DecisionClass,
    DecisionClassObservation,
    EvalSuite,
    EvaluationMetrics,
    EvaluationMode,
    EvidenceWindow,
    ExperimentArm,
    GateReason,
    GateReasonCode,
    GateResult,
    LearningPolicy,
    ProposalKind,
    ProposalStatus,
    TargetKind,
)
from ken_contracts.model import ModelProvider
from ken_core.config import Settings

__all__ = [
    "BEHAVIOURAL_REASON_CODES",
    "CODE_TOKEN_PATTERN",
    "EVALUATION_COUNTER_NAMES",
    "REFLECTION_COUNTER_NAMES",
    "BlockOnly",
    "EvaluationOutcome",
    "EvaluationRequest",
    "GateEvaluator",
    "PolicyFileWriter",
    "PromotionService",
    "ReflectionOutcome",
    "ReflectionRequest",
    "RunEvaluation",
    "RunReflection",
    "ShadowPass",
    "SqlExperimentGuard",
    "code_only_violations",
    "experiment_arm",
    "required_stages_met",
]

REFLECTION_COUNTER_NAMES: Final = frozenset(
    {
        "decision_classes",
        "samples_considered",
        "classes_below_sample_floor",
        "options_enumerated",
        "proposals_created",
        "proposals_deduplicated",
        "model_calls",
        "fallbacks",
    }
)
"""``job_runs.counters`` of every ``reflection`` run."""
EVALUATION_COUNTER_NAMES: Final = frozenset(
    {
        "suites_run",
        "cases_total",
        "cases_passed",
        "cases_failed",
        "must_not_violations",
        "classes_below_sample_floor",
        "model_calls",
        "fallbacks",
    }
)
"""``job_runs.counters`` of every ``evaluation`` run."""

CODE_TOKEN_PATTERN: Final = r"^[a-z0-9][a-z0-9_.|+-]{0,119}$"  # noqa: S105 -- a regex
"""The ONLY string shape allowed anywhere in a reflection prompt: an enum code, a dotted
policy path or a canonical decision class. It admits no capital letter, no space, no slash and
no colon, so a page title, an anchor phrase, a URL and a reviewer note are all excluded by
SHAPE rather than by the author remembering not to include them."""

_CODE_TOKEN = re.compile(CODE_TOKEN_PATTERN)
_MAX_PROMPT_DEPTH: Final = 8


def code_only_violations(payload: Any, *, path: str = "$") -> tuple[str, ...]:
    """JSON paths in ``payload`` that are not code, integers or floats (section 15.7.6).

    An empty result means the payload may be rendered into a reflection prompt. Every string
    -- value AND mapping key -- must match :data:`CODE_TOKEN_PATTERN`; ``bool``, ``int``,
    ``float`` and ``None`` always pass. Pure and total: it never raises, so a test and the
    renderer agree exactly.
    """
    return tuple(_walk(payload, path, 0))


def _walk(value: Any, path: str, depth: int) -> list[str]:
    if depth > _MAX_PROMPT_DEPTH:
        return [f"{path}: nested too deeply for a code-only payload"]
    if value is None or isinstance(value, bool | int | float):
        return []
    if isinstance(value, str):
        return [] if _CODE_TOKEN.fullmatch(value) else [f"{path}: not a code token"]
    if isinstance(value, Mapping):
        out: list[str] = []
        for key, item in value.items():
            if not isinstance(key, str) or not _CODE_TOKEN.fullmatch(key):
                out.append(f"{path}.{key!r}: key is not a code token")
            out.extend(_walk(item, f"{path}.{key}", depth + 1))
        return out
    if isinstance(value, Sequence):
        out = []
        for index, item in enumerate(value):
            out.extend(_walk(item, f"{path}[{index}]", depth + 1))
        return out
    return [f"{path}: {type(value).__name__} is not allowed in a prompt"]


def experiment_arm(*, key: str, source_page_id: UUID, treatment_fraction: float) -> ExperimentArm:
    """The arm ``make experiment-assign`` MATERIALISES for one page.

    Deterministic AND stored: the first 8 bytes of ``sha256(key + ':' + str(source_page_id))``
    as a big-endian integer, compared with ``treatment_fraction`` of the 64-bit space. Writing
    the row keeps the arm auditable and stable even if this function ever changes.
    """
    digest = hashlib.sha256(f"{key}:{source_page_id}".encode("ascii")).digest()[:8]
    position = int.from_bytes(digest, "big") / 2**64
    return ExperimentArm.TREATMENT if position < treatment_fraction else ExperimentArm.CONTROL


# --------------------------------------------------------------------------- #
# Behavioural signals: block-only, by construction (ADR-0016 15.7.12 / 16.7)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class BlockOnly:
    """The gate evaluator's ONLY route to behavioural data.

    First-party counts are spoofable (Origin and ``pv`` are attacker-chosen, ADR-0015 section
    12.3), GA4 has ``(other)`` rows and thresholding, and ``overlapping_changes`` flags most
    batch deployments. So a negative behavioural signal FAILS a gate and a positive one never
    passes one. That rule is code here rather than a row in a document, because otherwise
    ``AUTO_APPROVE`` could one day be reached on spoofable counts.

    The public surface is exactly :meth:`blocking_reasons`: there is no method that returns a
    :class:`~ken_contracts.learning.GateResult`, so this type structurally cannot pass a gate.
    A test asserts that the public API stays exactly that one method.
    """

    observations: tuple[DecisionClassObservation, ...]
    min_sample_count: int = MIN_SAMPLE_COUNT

    def blocking_reasons(self) -> tuple[GateReason, ...]:
        """Reasons the behavioural stream BLOCKS, in decision-class order. Possibly empty.

        Implemented by the gate evaluator (W2/W3); the signature is frozen here. It may return
        only :attr:`~ken_contracts.learning.GateReasonCode.BEHAVIOURAL_SIGNAL_NEGATIVE` and
        :attr:`~ken_contracts.learning.GateReasonCode.SAMPLE_TOO_SMALL`.
        """
        raise NotImplementedError


BEHAVIOURAL_REASON_CODES: Final = frozenset(
    {GateReasonCode.BEHAVIOURAL_SIGNAL_NEGATIVE, GateReasonCode.SAMPLE_TOO_SMALL}
)
"""The ONLY reason codes :meth:`BlockOnly.blocking_reasons` may return. There is deliberately
no code here that could contribute to a PASS."""


class GateEvaluator(Protocol):
    """``ken_services.learning.evaluate.evaluate_gate``: metrics + policy -> a verdict.

    ``pass`` requires every hard-fail suite clean, every declared suite at or above its
    ``min_pass_rate``, no regression beyond ``max_regression_vs_baseline``, and no decision
    class below ``min_sample_count`` -- a class below the floor makes the result
    ``inconclusive``, NEVER ``pass``. Behavioural reasons enter only through
    :class:`BlockOnly`.
    """

    def __call__(
        self,
        *,
        metrics: EvaluationMetrics,
        policy: LearningPolicy,
        baseline: EvaluationMetrics | None = None,
        behavioural: BlockOnly | None = None,
    ) -> tuple[GateResult, tuple[GateReason, ...]]: ...


# --------------------------------------------------------------------------- #
# Reflection (JobKind.REFLECTION, `make reflect`)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ReflectionRequest:
    window: EvidenceWindow | None = None
    """Observation window; ``None`` = the policy's default trailing window."""
    kinds: tuple[ProposalKind, ...] = (ProposalKind.POLICY,)
    target_kinds: tuple[TargetKind, ...] = ()
    """Empty = every target kind the change allowlist still covers."""
    use_model: bool = True
    """False forces the deterministic path (option 0 plus ``fallback_used``)."""
    triggered_by: str = "cli"


@dataclass(frozen=True, slots=True)
class ReflectionOutcome:
    job_run_id: UUID
    proposal_ids: tuple[UUID, ...] = ()
    deduplicated: int = 0
    counters: Mapping[str, int] = field(default_factory=dict)
    """Keys from :data:`REFLECTION_COUNTER_NAMES`."""


class RunReflection(Protocol):
    """``ken_services.learning.reflect.run_reflection``, executed as
    ``run_job(kind=JobKind.REFLECTION)``.

    Deterministic end to end except one bounded choice. ``model_provider`` is OPTIONAL: without
    it (or when it cannot answer, or answers invalidly) the deterministic rule picks option 0
    and the proposal records ``fallback_used`` with a ``FallbackReason``. The provider is only
    ever asked for a ``ProposalDraft``, and only ever sees a payload that
    :func:`code_only_violations` accepts.
    """

    async def __call__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        policy: LearningPolicy,
        request: ReflectionRequest,
        model_provider: ModelProvider | None = None,
        now: datetime | None = None,
    ) -> ReflectionOutcome: ...


class AggregateStreams(Protocol):
    """``ken_services.learning.aggregate.aggregate_streams``: the ONLY producer of
    :class:`~ken_contracts.learning.ProposalEvidence`.

    Every number is computed in SQL/Python over the three streams -- editorial
    (``review_actions``, ``link_plan_transitions``, ``agent_run_plans``), behavioural
    (``link_metrics_daily``, ``link_events``, ``link_measurements``) and search
    (``page_metrics_daily``). A static test asserts no other module constructs
    ``ProposalEvidence``, which is what makes "no model-produced number appears in evidence" a
    mechanism.
    """

    async def __call__(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        policy: LearningPolicy,
        window: EvidenceWindow,
    ) -> tuple[DecisionClassObservation, ...]: ...


class DecisionClassOf(Protocol):
    """``ken_services.learning.aggregate.decision_class_of``: the one place a plan row becomes
    a :class:`~ken_contracts.learning.DecisionClass`, so Phase 8 and Phase 9 key on the same
    canonical string."""

    def __call__(self, *, row: Mapping[str, Any]) -> DecisionClass: ...


# --------------------------------------------------------------------------- #
# Evaluation (JobKind.EVALUATION, `make eval`)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class EvaluationRequest:
    suites: tuple[EvalSuite, ...]
    mode: EvaluationMode = EvaluationMode.OFFLINE
    proposal_id: UUID | None = None
    skill_id: UUID | None = None
    """Exactly one of ``proposal_id`` / ``skill_id`` (a DB CHECK enforces it too)."""
    baseline_evaluation_id: UUID | None = None
    seed: int | None = None
    provider_name: str = "mock"
    """``mock`` replays recorded responses, so the same dataset gives a byte-identical report
    and CI can gate on it. ``ollama`` is opt-in (``KEN_RUN_LIVE_OLLAMA=1``) and never gates."""
    triggered_by: str = "cli"


@dataclass(frozen=True, slots=True)
class EvaluationOutcome:
    job_run_id: UUID
    evaluation_id: UUID
    gate_result: GateResult
    counters: Mapping[str, int] = field(default_factory=dict)
    """Keys from :data:`EVALUATION_COUNTER_NAMES`."""


class RunEvaluation(Protocol):
    """``ken_services.learning.evaluate.run_evaluation``, executed as
    ``run_job(kind=JobKind.EVALUATION)``.

    Writes exactly one ``policy_evaluations`` row recording the runner version, every dataset
    manifest sha256, the policy file shas, the provider, the RESOLVED model per role, the seed
    and the git sha -- so ``evaluation_ref`` points at something reproducible.
    ``mode=offline`` loads a candidate document IN MEMORY and replays the Phase 3 pipeline over
    the eval corpus; it never writes a policy file and never writes a ``link_plans`` row.
    """

    async def __call__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        policy: LearningPolicy,
        request: EvaluationRequest,
        model_provider: ModelProvider | None = None,
        now: datetime | None = None,
    ) -> EvaluationOutcome: ...


class ShadowPass(Protocol):
    """``ken_services.learning.shadow.shadow_pass``: after a real recommendation job, re-run
    selection with the candidate INSIDE THE SAME JOB, write NO ``link_plans``, and record one
    ``policy_evaluations`` row (``mode=shadow``) holding the plan-set diff -- pairs added,
    dropped, re-ordered, and the score deltas. Production output is unchanged, and a test
    asserts zero plan rows are written across the call."""

    async def __call__(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        policy: LearningPolicy,
        candidate_id: UUID,
        recommendation_run_id: UUID,
    ) -> UUID: ...


# --------------------------------------------------------------------------- #
# Promotion: the one write path (ADR-0016 15.7.2 / 15.7.3)
# --------------------------------------------------------------------------- #
class PromotionService(Protocol):
    """``ken_services.learning.promote.DatabasePromotionService``.

    It takes a :class:`~ken_contracts.auth.Principal` and NEVER an actor string: the actor is
    ``principal.actor``, derived from the session (Phase 3 QA H3, ADR-0013's review-service
    rule). The API passes the principal that ``Permission.PROMOTE`` admitted.

    ``advance`` is the ONLY code path that may write ``learning_proposal_transitions``, and the
    only one that may set ``ken.learning_promoter``. In ONE transaction it: re-reads the
    proposal ``FOR UPDATE``; refuses a replay whose ``operation_id`` matches an earlier call
    with a different body; checks the transition is in ``PROPOSAL_TRANSITIONS`` and that the
    principal's class may reach it; for ``promoted`` additionally checks, IN THIS ORDER,

    1. ``Settings.learning_demonstration_mode`` is off (else ``DEMONSTRATION_MODE``),
    2. the gate policy's ``required_stages`` for this kind are all present in the proposal's
       transition history,
    3. the evaluation run exists, belongs to this proposal or skill, has
       ``gate_result = pass``, matches the proposal's CURRENT fingerprint, has unchanged
       dataset manifests, and is newer than ``promotion_max_eval_age_days``;

    then sets the promoter GUC, INSERTs the transition, UPDATEs the proposal, and emits
    ``policy.promoted`` / ``skill.promoted`` with ``promoted_by = principal.actor`` and
    ``evaluation_ref = str(evaluation_run_id)``. It writes NO policy file: only the CLI does
    that, and only after this transition exists.

    Raises:
        PromotionRefusedError: with the :class:`~ken_contracts.learning.PromotionRefusal` code.
        ProposalNotFoundError: no such proposal.
    """

    async def advance(
        self, *, principal: Principal, proposal_id: UUID, request: AdvanceProposalRequest
    ) -> AdvanceProposalResponse: ...


class PolicyFileWriter(Protocol):
    """``make policy-promote`` (``ken_services.learning.cli``). The ONLY file write of the phase.

    It REFUSES unless a ``promoted`` transition with a ``^user:`` actor and an
    ``evaluation_run_id`` already exists for the proposal, and it never creates one
    (section 15.7.9): the API records the decision, the CLI materialises the file.

    For ``ranking`` the write is TWO files and must be ATOMIC (section 15.7.8):
    ``policies/ranking/ranking_weights.v<N+1>.yaml`` AND a new recommendation document whose
    ``ranking_policy:`` pointer names it, because ``resolve_ranking_policy`` reads the active
    ranking policy from that pointer and not from ``Settings``. A previously released file is
    NEVER edited or deleted, so a past decision stays reproducible against the exact weights
    that produced it. Promotion changes the active policy path, and ``Settings`` is frozen, so
    it needs an operator restart; hot reload would be a material architecture change.

    Returns the repository-relative paths written, in write order.
    """

    async def __call__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        proposal_id: UUID,
        dry_run: bool = True,
    ) -> tuple[str, ...]: ...


# --------------------------------------------------------------------------- #
# Experiments (plumbing only -- ADR-0016 16.6)
# --------------------------------------------------------------------------- #
class SqlExperimentGuard(Protocol):
    """``ken_services.learning.experiments.SqlExperimentGuard``: the real
    :class:`~ken_contracts.recommendations.ExperimentGuard` replacing ``NoExperiments()``.

    Its contract is PRD 7.5's "an active experiment forbids modification". A ``control``
    assignment in an ACTIVE experiment whose kind withholds links locks the source page; a
    ``policy_ab`` experiment locks NOBODY, because both arms get links and only the ranking
    policy differs -- the pipeline resolves the policy per source from the arm.

    Since ``experiments.kind`` is capped to ``policy_ab`` (ADR-0016 16.4 cut ``holdout``
    entirely), :meth:`locked` returns ``False`` for every pair in Phase 8 BY CONSTRUCTION, not
    by omission. That is the point: the hook is wired, the tables are real, and adding a
    withholding kind later is a schema change a human must make deliberately.

    Backed by a per-run cached assignment map, never per-pair SQL (one round trip per candidate
    would be a real cost). ``refresh`` rebuilds it; a failure keeps the previous map and never
    raises, so an experiment lookup can never fail a recommendation run.
    """

    async def locked(self, *, source_page_id: UUID, target_page_id: UUID) -> bool: ...

    async def refresh(self) -> None: ...


class ExperimentAttribution(Protocol):
    """``ken_services.learning.experiments.attribute``: arm membership x the experiment's
    ``[started_at, ended_at)`` x the plan's live window from ``link_plan_transitions``.

    Always DERIVED. No experiment column is ever added to ``link_events`` -- a column-set test
    already pins that table's eight columns.

    PERMITTED claim: "observed difference between arms over [interval], N_control/N_treatment
    pages, with these confounder flags". FORBIDDEN: statistical significance, causal lift from
    first-party counts, revenue, ranking causation. The UI carries the same "observed change,
    not a causal effect" note Phase 7 uses.
    """

    async def __call__(
        self, session: AsyncSession, *, experiment_id: UUID
    ) -> Mapping[ExperimentArm, tuple[UUID, ...]]: ...


def required_stages_met(
    *, policy: LearningPolicy, key: str, reached: Sequence[ProposalStatus]
) -> bool:
    """Whether every stage the gate policy requires for ``key`` has been reached.

    ``key`` is a :class:`~ken_contracts.learning.TargetKind` value for a policy proposal, or
    ``"skill"`` for a skill. Implemented here so the promotion service and the UI's
    "what is still missing" list agree exactly.
    """
    required = policy.gates.required_stages.get(key, ())
    return set(required) <= set(reached)
