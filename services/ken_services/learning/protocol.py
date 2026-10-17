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
    ExperimentKind,
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
    "ExperimentLifecycle",
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


def experiment_arm(
    *, kind: ExperimentKind, key: str, page_id: UUID, fraction: float
) -> ExperimentArm:
    """The arm ``make experiment-assign`` MATERIALISES for one page.

    Deterministic AND stored: the first 8 bytes of ``sha256(key + ':' + str(page_id))`` as a
    big-endian integer, as a position in ``[0, 1)``. Writing the row keeps the arm auditable and
    stable even if this function ever changes.

    **``fraction`` means the share of pages in the arm that is CAPPED, and that is not the same
    arm for both kinds -- which is why ``kind`` is a required argument of this one function:**

    * ``policy_ab``: the share in the TREATMENT arm (the candidate ranking policy). Both arms
      still receive links; only the ranking differs.
    * ``holdout``: the share in the CONTROL arm, i.e. the pages WITHHELD from new inbound
      links. The database caps it at 0.20 precisely because it is the withheld share, and
      :class:`SqlExperimentGuard` locks the control arm. Reading it
      as a *treatment* share would put ``1 - fraction`` of the site in the withheld arm -- a
      "10 %" holdout would withhold links from about 90 % of pages and make the cap
      meaningless. (The column is still named ``treatment_fraction``; the name predates this
      correction and is kept so the schema need not change.)

    ``page_id`` is the SOURCE page for a ``policy_ab`` and the TARGET page for a ``holdout``
    (:class:`~ken_contracts.learning.ExperimentUnit`), which is the whole fix for the review's
    arm-contamination bug: the pages an experiment measures are the pages it assigns.
    """
    digest = hashlib.sha256(f"{key}:{page_id}".encode("ascii")).digest()[:8]
    position = int.from_bytes(digest, "big") / 2**64
    if kind is ExperimentKind.HOLDOUT:
        return ExperimentArm.CONTROL if position < fraction else ExperimentArm.TREATMENT
    return ExperimentArm.TREATMENT if position < fraction else ExperimentArm.CONTROL


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

    Its contract is PRD 7.5's "an active experiment forbids modification".
    (A correction to ADR-0016 section 1, recorded in its 15.1.2:
    ``ken_contracts.recommendations.ExperimentGuard`` is a ``@runtime_checkable Protocol``, not
    "a frozen protocol" -- so a structural implementation satisfies it and no base class is
    inherited.)

    **The rule, stated exactly** (this is the resolution of the arm-contamination bug the
    pre-fan-out review found, ADR-0016 15.5, and it is the reason the frozen
    ``ExperimentGuard.locked(*, source_page_id, target_page_id)`` signature takes BOTH pages):

        ``locked()`` is true iff ``target_page_id`` has a ``control`` assignment in an ACTIVE
        ``holdout`` experiment.

    A ``holdout`` exists to ask "withhold inbound links from these pages and compare their
    search performance", so its arms are assigned BY TARGET PAGE
    (:class:`~ken_contracts.learning.ExperimentUnit.TARGET_PAGE`, pinned to the kind by a DB
    CHECK) and the guard refuses any plan pointing AT a control page -- whatever its source.
    The draft assigned arms by source page while measuring target pages, which left control
    pages free to receive inbound links from unassigned sources: the arms were contaminated and
    the comparison meaningless. ``source_page_id`` is still accepted, and still used, because a
    later kind may withhold outbound links instead; today it affects nothing.

    A ``policy_ab`` locks NOBODY: both arms get links and only the ranking policy differs, which
    the pipeline resolves per source from that source's arm.

    Backed by a per-run cached assignment map, never per-pair SQL (one round trip per candidate
    would be a real cost). ``refresh`` rebuilds it; a failure keeps the previous map and never
    raises, so an experiment lookup can never fail a recommendation run.
    """

    async def locked(self, *, source_page_id: UUID, target_page_id: UUID) -> bool: ...

    async def refresh(self) -> None: ...


class ExperimentLifecycle(Protocol):
    """``ken_services.learning.experiments.ExperimentLifecycleService``: create, assign, activate,
    end. CLI-only -- there is no API route that starts an experiment.

    **Activating a holdout is the one genuinely destructive act in this phase**: its control
    pages are deliberately denied inbound links on the client's live site. Three gates, each a
    mechanism:

    1. ``treatment_fraction <= MAX_HOLDOUT_TREATMENT_FRACTION`` (0.20) -- a DB CHECK, so no
       policy file and no caller can raise it. The policy default is 0.10.
    2. :meth:`activate` raises
       :class:`~ken_contracts.learning.ExperimentRefusedError` with
       :attr:`~ken_contracts.learning.ExperimentRefusal.HOLDOUT_NOT_ENABLED` while
       ``Settings.experiments_allow_holdout`` is unset, which is the default.
    3. Only when that setting is on does it run
       ``SELECT set_config('ken.learning_experiment_activator', 'on', true)`` in the SAME
       transaction as the ``draft -> active`` UPDATE. Migration 0009's guard requires it for a
       ``holdout``, so a raw ``UPDATE experiments SET status = 'active'`` fails too --
       "the user has not enabled holdouts" is enforced by the database, not only by a branch.

    A ``policy_ab`` activation is unaffected by the setting and needs no GUC: both arms get
    links. :meth:`activate` still refuses a non-draft, a transition outside the ladder and an
    experiment with no materialised assignments (activating with no arms would silently measure
    nothing).

    :meth:`assign` materialises :func:`experiment_arm` for every eligible page of the
    experiment's ``unit``, while the experiment is still ``draft``. Its ``actor`` is a
    ``system:`` actor, NOT a human one: the arm is a deterministic computation, not a judgement,
    and the human act is recorded on ``experiments.created_by`` (CHECKed ``^user:``) and on the
    activation. The static test that forbids a ``"user:"`` literal in this package is what stops
    an implementation from passing a forged human actor here.
    """

    async def assign(
        self, *, key: str, page_ids: Sequence[UUID] | None = None, actor: str
    ) -> Mapping[ExperimentArm, int]: ...

    async def activate(self, *, principal: Principal, key: str) -> None: ...

    async def end(self, *, principal: Principal, key: str) -> None: ...


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
