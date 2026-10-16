"""``run_reflection``: corpus-level reflection as a DETERMINISTIC JOB (W1).

Implements the frozen :class:`~ken_services.learning.protocol.RunReflection`, executed as
``run_job(kind=JobKind.REFLECTION)`` behind ``make reflect``. It is a job and not an
agent-graph node because the graph is per-source-page and terminates at hand-off (ADR-0012)
while reflection spans the corpus -- and because CLAUDE.md says to prefer deterministic code
over LLM reasoning wherever possible.

**Where the model sits, exactly.** Deterministic code does all of this:

1. aggregates the three streams (:mod:`ken_services.learning.aggregate`);
2. keeps only decision classes at or above the policy's sample floor -- below it there is
   nothing to learn, and the run reports ``inconclusive`` rather than a confident proposal;
3. derives the editorial PRESSURES (reason-code counts, reject rate) from those classes;
4. enumerates a small, ordered set of candidate changes from those pressures, strongest first,
   with ``keep_current`` LAST;
5. validates each candidate against the frozen contract, the narrowed policy allowlist, the
   ADR-0012 cross-check and the real policy model.

Only then, and only when there is a genuine choice to make, is a model asked -- for a
:class:`~ken_contracts.learning.ProposalDraft` holding a title, a rationale and an INDEX.
``choice_index`` is re-checked against the LIVE option count (ADR-0016 15.7.7) and the full
option set is persisted in the evidence, so the model:

* never emits a number that is stored,
* never names a field or a path,
* cannot reach a frozen path, because it cannot reach any path.

**Option 0 is the deterministic rule's own preferred candidate**, not "do nothing": a disabled,
unreachable or invalid model therefore falls back to the DETERMINISTIC decision and records
``fallback_used`` with a ``FallbackReason`` -- exactly as the agent nodes do, and exactly what
makes the model genuinely optional rather than load-bearing. ``keep_current`` is the last
option, so a model that reads the table as inconclusive can still choose to change nothing, and
then no proposal is written at all.

**The prompt carries enum codes, integers and floats only** (ADR-0016 15.7.6).
:mod:`ken_services.learning.prompt` enforces it with the frozen ``code_only_violations``
whitelist and refuses to render anything else; that refusal is handled here as a fallback, so a
"nearly clean" prompt is never sent.

Nothing in this module promotes, advances or withdraws anything. It inserts ``proposed`` rows
and emits ``policy.proposed``; every stage after that needs a human (ADR-0016 15.7.1-15.7.3).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.agents import FallbackReason
from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import JobKind
from ken_contracts.learning import (
    MAX_OPTIONS_PER_PROPOSAL,
    MAX_RATIONALE_CHARS,
    MAX_TITLE_CHARS,
    CrossPolicyContext,
    DecisionClassObservation,
    LearningPolicy,
    PolicyChange,
    ProposalDraft,
    ProposalKind,
    ProposalOption,
    TargetKind,
)
from ken_contracts.model import (
    GenerationOptions,
    ModelProvider,
    ModelRole,
    ModelSchemaViolationError,
    ModelTimeoutError,
    ModelUnavailableError,
)
from ken_contracts.recommendations import RANKING_COMPONENTS
from ken_core.config import REPO_ROOT, Settings
from ken_core.logging import get_logger
from ken_services.jobs.runner import JobContext, run_job, utc_now
from ken_services.learning.aggregate import (
    AggregateReport,
    aggregate_report,
    build_evidence,
    default_window,
)
from ken_services.learning.prompt import KEEP_CURRENT, PromptPayloadError, reflection_messages
from ken_services.learning.proposals import (
    AGENTS_PER_RUN_PATH,
    RECOMMENDATION_PER_SOURCE_PATH,
    CandidateRefusedError,
    insert_policy_proposal,
    policy_change,
    prepare_policy_proposal,
)
from ken_services.learning.protocol import (
    REFLECTION_COUNTER_NAMES,
    ReflectionOutcome,
    ReflectionRequest,
)

__all__ = [
    "CONFIDENCE_STEP",
    "KEEP_CURRENT",
    "MIN_RESIDUAL_WEIGHT",
    "REFLECTION_TIMEOUT_SECONDS",
    "REJECT_PRESSURE",
    "SCORE_STEP",
    "WEIGHT_STEP",
    "OptionSpec",
    "ReflectionOutcome",
    "ReflectionRequest",
    "SkillReflectionUnsupportedError",
    "enumerate_options",
    "run_reflection",
]

logger = get_logger(__name__)

WEIGHT_STEP: Final = 0.05
"""One ranking-weight shift. A step, not a model's number: the model picks among steps."""
MIN_RESIDUAL_WEIGHT: Final = 0.02
"""A donor component keeps at least this much weight, so a shift can never zero a signal."""
SCORE_STEP: Final = 0.05
CONFIDENCE_STEP: Final = 0.05
REJECT_PRESSURE: Final = 0.25
"""Reject rate over the SUFFICIENT classes above which selectivity candidates are offered."""
REFLECTION_TIMEOUT_SECONDS: Final = 60.0
"""A code constant: ``Settings`` has no model timeout and the agents policy's node timeout
belongs to the graph, not to a batch job."""

_DESTINATION_REASONS: Final = (
    "irrelevant_destination",
    "better_destination_exists",
    "reader_journey_mismatch",
)
_JOURNEY_REASON: Final = "reader_journey_mismatch"
_OVER_LINKING_REASON: Final = "over_linking"
_DUPLICATE_REASON: Final = "duplicate_of_existing_link"

_RANKING_TAXONOMY: Final = "ranking.weights.taxonomy"
_RANKING_INTENT: Final = "ranking.weights.intent"
_MIN_SCORE_PATH: Final = "recommendation.selection.min_score"
_OPEN_PER_TARGET_PATH: Final = "recommendation.selection.max_open_plans_per_target"
_CRITIC_CONFIDENCE_PATH: Final = "agents.critic.accept_min_confidence"


class SkillReflectionUnsupportedError(ValueError):
    """Reflection proposes POLICIES only in Phase 8.

    A skill's content is free text (``instructions``), so a model-authored skill would be a
    stored model string -- precisely what the bounded-role rule exists to prevent (ADR-0016
    15.2d). A skill proposal is therefore human-authored for now, and ADR-0016 16.5 already
    records that nothing consumes a skill at runtime in this phase.
    """

    def __init__(self) -> None:
        super().__init__("reflection proposes policy changes only in Phase 8")


# --------------------------------------------------------------------------- #
# Deterministic option enumeration
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class OptionSpec:
    """One candidate the deterministic rule offers. ``title`` is author-written, never model
    text: it is the heading a human sees if the model never answers."""

    label: str
    title: str
    changes: tuple[tuple[str, Any], ...] = ()
    strength: float = 0.0
    """How strongly the evidence points at this candidate, as an INCIDENCE PER SAMPLE so the
    candidates are comparable: a raw reason COUNT and a reject RATE are different units, and
    ordering options by incomparable numbers would make option 0 arbitrary. Orders the option
    set and is never stored -- what is stored is the resulting order."""


@dataclass(frozen=True, slots=True)
class _Pressures:
    """The editorial signal, summed over the SUFFICIENT classes only.

    Every number here is a field of a stored ``DecisionClassObservation``, so no option is ever
    derived from a quantity the evidence does not carry -- which is what lets the
    ``reflection-grounding`` suite re-derive a proposal's justification from the database.
    """

    samples: int = 0
    rejections: int = 0
    destination: int = 0
    journey: int = 0
    over_linking: int = 0
    duplicate: int = 0

    def _per_sample(self, count: int) -> float:
        return 0.0 if self.samples == 0 else count / self.samples

    @property
    def reject_rate(self) -> float:
        return self._per_sample(self.rejections)

    @property
    def destination_rate(self) -> float:
        return self._per_sample(self.destination)

    @property
    def journey_rate(self) -> float:
        return self._per_sample(self.journey)

    @property
    def over_linking_rate(self) -> float:
        return self._per_sample(self.over_linking)

    @property
    def duplicate_rate(self) -> float:
        return self._per_sample(self.duplicate)


def _pressures(observations: Sequence[DecisionClassObservation]) -> _Pressures:
    samples = sum(row.sample_count for row in observations)
    rejections = sum(row.rejections for row in observations)
    counts: dict[str, int] = {}
    for row in observations:
        for code, count in row.reason_histogram.items():
            counts[code] = counts.get(code, 0) + count
    return _Pressures(
        samples=samples,
        rejections=rejections,
        destination=sum(counts.get(code, 0) for code in _DESTINATION_REASONS),
        journey=counts.get(_JOURNEY_REASON, 0),
        over_linking=counts.get(_OVER_LINKING_REASON, 0),
        duplicate=counts.get(_DUPLICATE_REASON, 0),
    )


def _weight_donor(weights: Mapping[str, float], receiver: str) -> str | None:
    """The largest OTHER component that can give up :data:`WEIGHT_STEP`.

    Deterministic: ties break on the component name, so the same weights always pick the same
    donor and the same proposal fingerprints out.
    """
    eligible = [
        name
        for name in RANKING_COMPONENTS
        if name != receiver and weights.get(name, 0.0) - WEIGHT_STEP >= MIN_RESIDUAL_WEIGHT
    ]
    if not eligible:
        return None
    return min(eligible, key=lambda name: (-weights[name], name))


def _shift(weights: Mapping[str, float], *, receiver: str, donor: str) -> tuple[float, float]:
    """``(new receiver weight, new donor weight)`` for one :data:`WEIGHT_STEP` shift.

    The donor is computed as ``1 - sum(everything else)`` rather than ``w - step``, so the
    candidate's weights sum to 1 within ONE rounding error. Rounding both ends independently
    can drift by 2e-6, which the ranking policy's own 1e-6 tolerance would reject -- a
    candidate that cannot load is not a candidate.
    """
    new_receiver = round(weights[receiver] + WEIGHT_STEP, 6)
    others = sum(
        new_receiver if name == receiver else weights[name]
        for name in RANKING_COMPONENTS
        if name != donor
    )
    return new_receiver, round(1.0 - others, 6)


def _ranking_specs(document: Mapping[str, Any], pressures: _Pressures) -> list[OptionSpec]:
    weights = {name: float(value) for name, value in (document.get("weights") or {}).items()}
    if set(weights) != set(RANKING_COMPONENTS):
        return []
    specs: list[OptionSpec] = []
    for receiver, path, title, strength in (
        (
            "taxonomy",
            _RANKING_TAXONOMY,
            "Shift ranking weight towards the explainable taxonomy relationship",
            pressures.destination_rate,
        ),
        (
            "intent",
            _RANKING_INTENT,
            "Shift ranking weight towards reader-intent compatibility",
            pressures.journey_rate,
        ),
    ):
        if strength <= 0:
            continue
        donor = _weight_donor(weights, receiver)
        if donor is None:
            continue
        new_receiver, new_donor = _shift(weights, receiver=receiver, donor=donor)
        if new_donor < MIN_RESIDUAL_WEIGHT or new_receiver > 1.0:
            continue
        specs.append(
            OptionSpec(
                label=f"shift_weight_to_{receiver}",
                title=title,
                changes=((path, new_receiver), (f"ranking.weights.{donor}", new_donor)),
                strength=strength,
            )
        )
    return specs


def _recommendation_specs(
    document: Mapping[str, Any], pressures: _Pressures, cross: CrossPolicyContext
) -> list[OptionSpec]:
    selection = document.get("selection") or {}
    specs: list[OptionSpec] = []
    min_score = float(selection.get("min_score", 0.0))
    if pressures.reject_rate >= REJECT_PRESSURE and min_score + SCORE_STEP <= 1.0:
        specs.append(
            OptionSpec(
                label="raise_min_score",
                title="Raise the minimum final score a link must reach to be proposed",
                changes=((_MIN_SCORE_PATH, round(min_score + SCORE_STEP, 6)),),
                strength=pressures.reject_rate,
            )
        )
    per_source = int(selection.get("max_plans_per_source", 1))
    if pressures.over_linking > 0 and per_source - 1 >= max(1, cross.agents_max_plans_per_run):
        specs.append(
            OptionSpec(
                label="lower_max_plans_per_source",
                title="Propose fewer links per source page",
                changes=((RECOMMENDATION_PER_SOURCE_PATH, per_source - 1),),
                strength=pressures.over_linking_rate,
            )
        )
    per_target = int(selection.get("max_open_plans_per_target", 1))
    if pressures.duplicate > 0 and per_target > 1:
        specs.append(
            OptionSpec(
                label="lower_max_open_plans_per_target",
                title="Allow fewer open plans pointing at one destination",
                changes=((_OPEN_PER_TARGET_PATH, per_target - 1),),
                strength=pressures.duplicate_rate,
            )
        )
    return specs


def _agents_specs(document: Mapping[str, Any], pressures: _Pressures) -> list[OptionSpec]:
    specs: list[OptionSpec] = []
    threshold = float((document.get("critic") or {}).get("accept_min_confidence", 0.0))
    if pressures.reject_rate >= REJECT_PRESSURE and threshold + CONFIDENCE_STEP <= 1.0:
        specs.append(
            OptionSpec(
                label="raise_critic_accept_min_confidence",
                title="Require more confidence before the critic accepts a link",
                changes=((_CRITIC_CONFIDENCE_PATH, round(threshold + CONFIDENCE_STEP, 6)),),
                strength=pressures.reject_rate,
            )
        )
    per_run = int(document.get("max_plans_per_run", 1))
    if pressures.over_linking > 0 and per_run > 1:
        specs.append(
            OptionSpec(
                label="lower_max_plans_per_run",
                title="Review fewer links per source page in one agent run",
                changes=((AGENTS_PER_RUN_PATH, per_run - 1),),
                strength=pressures.over_linking_rate,
            )
        )
    return specs


def enumerate_options(
    *,
    target_kind: TargetKind,
    base_document: Mapping[str, Any],
    observations: Sequence[DecisionClassObservation],
    policy: LearningPolicy,
    cross_check: CrossPolicyContext,
) -> tuple[tuple[ProposalOption, ...], tuple[OptionSpec, ...]]:
    """The server-enumerated option set for one policy, strongest candidate first.

    Only classes at or above ``min_sample_count`` contribute, so a corpus below the floor
    produces exactly one option (``keep_current``) and no model is ever consulted.

    Returns the frozen options and the specs behind them, index for index. A candidate the
    boundary refuses -- frozen, de-allowlisted by the policy file, cross-check breaking, or
    producing an invalid document -- is DROPPED and the remainder re-indexed, so
    ``ProposalOption.index`` always equals its position.
    """
    sufficient = [row for row in observations if row.sufficient]
    pressures = _pressures(sufficient)
    specs: list[OptionSpec] = []
    if sufficient:
        match target_kind:
            case TargetKind.RANKING:
                specs = _ranking_specs(base_document, pressures)
            case TargetKind.RECOMMENDATION:
                specs = _recommendation_specs(base_document, pressures, cross_check)
            case TargetKind.AGENTS:
                specs = _agents_specs(base_document, pressures)
    specs.sort(key=lambda spec: (-spec.strength, spec.label))
    specs = specs[: MAX_OPTIONS_PER_PROPOSAL - 1]
    specs.append(
        OptionSpec(label=KEEP_CURRENT, title="Change nothing: the evidence does not support it")
    )

    options: list[ProposalOption] = []
    kept: list[OptionSpec] = []
    for spec in specs:
        changes: list[PolicyChange] = []
        try:
            for path, value in spec.changes:
                changes.append(
                    policy_change(
                        path=path,
                        base_document=base_document,
                        to_value=value,
                        target_kind=target_kind,
                        policy=policy,
                    )
                )
        except CandidateRefusedError as exc:
            logger.info(
                "reflection_option_refused",
                target_kind=target_kind.value,
                label=spec.label,
                refusal=exc.refusal.value,
                path=exc.path,
            )
            continue
        options.append(ProposalOption(index=len(options), label=spec.label, changes=tuple(changes)))
        kept.append(spec)
    return tuple(options), tuple(kept)


# --------------------------------------------------------------------------- #
# The bounded model slot
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class _Choice:
    index: int
    title: str
    rationale: str
    model: str | None = None
    fallback_reason: FallbackReason | None = None


def _deterministic_choice(spec: OptionSpec, reason: FallbackReason) -> _Choice:
    """Option 0 -- the deterministic rule's own preferred candidate -- with a fixed rationale.

    The rationale is author-written and names only the fallback CODE: never exception text,
    never a model string, never anything read from the database.
    """
    return _Choice(
        index=0,
        title=spec.title[:MAX_TITLE_CHARS],
        rationale=(
            "Chosen by the deterministic rule, because no model answer was used "
            f"(fallback: {reason.value}). The option set, the observation window and every "
            "count behind this proposal are in its evidence; a human reviews all of it before "
            "anything changes."
        ),
        model=None,
        fallback_reason=reason,
    )


async def _choose(
    *,
    provider: ModelProvider | None,
    use_model: bool,
    report: AggregateReport,
    target_kind: TargetKind,
    options: Sequence[ProposalOption],
    specs: Sequence[OptionSpec],
    policy: LearningPolicy,
) -> tuple[_Choice, int]:
    """Pick one option. Returns the choice and how many model calls it cost (0 or 1)."""
    if not use_model:
        return _deterministic_choice(specs[0], FallbackReason.MODEL_DISABLED), 0
    if provider is None:
        return _deterministic_choice(specs[0], FallbackReason.MODEL_UNAVAILABLE), 0
    try:
        messages = reflection_messages(
            report=report, target_kind=target_kind, options=options, policy=policy
        )
    except PromptPayloadError as exc:
        logger.warning(
            "reflection_prompt_refused",
            target_kind=target_kind.value,
            violations=list(exc.violations[:10]),
        )
        return _deterministic_choice(specs[0], FallbackReason.SCHEMA_VIOLATION), 0

    try:
        async with asyncio.timeout(REFLECTION_TIMEOUT_SECONDS):
            result = await provider.complete_structured(
                role=ModelRole.REASONING,
                messages=messages,
                schema=ProposalDraft,
                options=GenerationOptions(temperature=0.0, timeout_s=REFLECTION_TIMEOUT_SECONDS),
            )
    except (TimeoutError, ModelTimeoutError):
        return _deterministic_choice(specs[0], FallbackReason.MODEL_TIMEOUT), 1
    except (ModelSchemaViolationError, ValidationError):
        return _deterministic_choice(specs[0], FallbackReason.SCHEMA_VIOLATION), 1
    except ModelUnavailableError:
        return _deterministic_choice(specs[0], FallbackReason.MODEL_UNAVAILABLE), 1
    except Exception as exc:  # a model problem never fails a job run
        logger.warning(
            "reflection_model_failed", target_kind=target_kind.value, detail=type(exc).__name__
        )
        return _deterministic_choice(specs[0], FallbackReason.MODEL_UNAVAILABLE), 1

    draft = result.value
    # ADR-0016 15.7.7: the index is validated against the LIVE option count, not against the
    # count the model claims. A model that under-reports option_count cannot widen its reach.
    if draft.option_count != len(options) or draft.choice_index >= len(options):
        logger.warning(
            "reflection_choice_out_of_range",
            target_kind=target_kind.value,
            offered=len(options),
            claimed=draft.option_count,
            chosen=draft.choice_index,
        )
        return _deterministic_choice(specs[0], FallbackReason.SCHEMA_VIOLATION), 1
    return (
        _Choice(
            index=draft.choice_index,
            title=draft.title[:MAX_TITLE_CHARS],
            rationale=draft.rationale[:MAX_RATIONALE_CHARS],
            model=result.completion.model,
        ),
        1,
    )


# --------------------------------------------------------------------------- #
# Base documents and the cross-policy context
# --------------------------------------------------------------------------- #
def _base_documents(
    settings: Settings,
) -> tuple[dict[TargetKind, dict[str, Any]], CrossPolicyContext]:
    """The three live policy documents as VALIDATED dumps, plus the cross-check context."""
    from ken_services.agents.policy import load_configured_agent_policy
    from ken_services.recommendations.policy import (
        load_recommendation_policy,
        resolve_ranking_policy,
    )

    recommendation = load_recommendation_policy(Path(settings.recommendation_policy_path))
    ranking = resolve_ranking_policy(recommendation, root=REPO_ROOT)
    agents = load_configured_agent_policy(settings)
    documents = {
        TargetKind.RANKING: ranking.model_dump(mode="json"),
        TargetKind.RECOMMENDATION: recommendation.model_dump(mode="json"),
        TargetKind.AGENTS: agents.model_dump(mode="json"),
    }
    context = CrossPolicyContext(
        agents_max_plans_per_run=agents.max_plans_per_run,
        recommendation_max_plans_per_source=recommendation.selection.max_plans_per_source,
    )
    return documents, context


# --------------------------------------------------------------------------- #
# The job
# --------------------------------------------------------------------------- #
async def run_reflection(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    policy: LearningPolicy,
    request: ReflectionRequest,
    model_provider: ModelProvider | None = None,
    now: datetime | None = None,
) -> ReflectionOutcome:
    """Run one reflection pass. Implements the frozen ``RunReflection``.

    Raises:
        SkillReflectionUnsupportedError: ``request.kinds`` asks for a skill proposal.
        JobAlreadyRunningError: another reflection run holds the kind lock.
    """
    if ProposalKind.SKILL in request.kinds:
        raise SkillReflectionUnsupportedError

    def clock() -> datetime:
        """The run's clock. A fixed ``now`` makes a whole pass reproducible."""
        return utc_now() if now is None else now

    window = request.window or default_window(clock())
    targets = request.target_kinds or tuple(TargetKind)
    documents, cross_check = _base_documents(settings)

    created: list[uuid.UUID] = []
    deduplicated = 0

    async def body(job: JobContext) -> RunStatus:
        nonlocal deduplicated
        async with job.transaction() as tx:
            report = await aggregate_report(
                tx.session, settings=settings, policy=policy, window=window
            )
            tx.increment("decision_classes", len(report.rows))
            tx.increment("samples_considered", report.sample_count_total)
            tx.increment("classes_below_sample_floor", report.classes_below_floor)

        for target_kind in targets:
            options, specs = enumerate_options(
                target_kind=target_kind,
                base_document=documents[target_kind],
                observations=report.observations,
                policy=policy,
                cross_check=cross_check,
            )
            job.increment("options_enumerated", len(options))
            if len(options) < 2:
                # Only `keep_current` survived: nothing to choose, so nothing is asked and
                # nothing is proposed. This is what the synthetic corpus produces.
                logger.info(
                    "reflection_inconclusive",
                    target_kind=target_kind.value,
                    decision_classes=len(report.rows),
                    classes_below_sample_floor=report.classes_below_floor,
                )
                continue
            choice, calls = await _choose(
                provider=model_provider,
                use_model=request.use_model,
                report=report,
                target_kind=target_kind,
                options=options,
                specs=specs,
                policy=policy,
            )
            job.increment("model_calls", calls)
            if choice.fallback_reason is not None:
                job.increment("fallbacks")
            chosen = options[choice.index]
            if not chosen.changes:
                logger.info(
                    "reflection_no_action", target_kind=target_kind.value, label=chosen.label
                )
                continue
            try:
                prepared = prepare_policy_proposal(
                    target_kind=target_kind,
                    base_document=documents[target_kind],
                    changes=chosen.changes,
                    cross_check=cross_check,
                )
            except CandidateRefusedError as exc:
                logger.warning(
                    "reflection_candidate_refused",
                    target_kind=target_kind.value,
                    refusal=exc.refusal.value,
                    path=exc.path,
                )
                continue
            evidence = build_evidence(
                report=report,
                options=options,
                chosen_index=choice.index,
                generated_at=clock(),
                model=choice.model,
                fallback_reason=choice.fallback_reason,
            )
            proposal_id: uuid.UUID | None = None
            try:
                async with job.transaction() as tx:
                    proposal_id = await insert_policy_proposal(
                        tx.session,
                        prepared=prepared,
                        evidence=evidence,
                        title=choice.title,
                        rationale=choice.rationale,
                        job_run_id=job.run_id,
                        now=clock(),
                    )
                    if proposal_id is None:
                        tx.increment("proposals_deduplicated")
                    else:
                        tx.increment("proposals_created")
            except IntegrityError:
                # The partial UNIQUE index on open fingerprints is the real idempotence; a
                # racing re-run lands here and is reported, never raised.
                deduplicated += 1
                job.increment("proposals_deduplicated")
                logger.info(
                    "learning_proposal_deduplicated_by_index",
                    fingerprint=prepared.fingerprint,
                )
                continue
            if proposal_id is None:
                deduplicated += 1
            else:
                created.append(proposal_id)
        return RunStatus.SUCCEEDED

    detail = await run_job(
        kind=JobKind.REFLECTION,
        sessionmaker=sessionmaker,
        triggered_by=request.triggered_by,
        body=body,
        config_snapshot={
            "window_start": window.start.isoformat(),
            "window_end": window.end.isoformat(),
            "target_kinds": [kind.value for kind in targets],
            "use_model": request.use_model,
            "learning_policy_version": policy.version,
            "min_sample_count": policy.gates.min_sample_count,
            "demonstration_mode": settings.learning_demonstration_mode,
        },
        counter_names=sorted(REFLECTION_COUNTER_NAMES),
        clock=clock,
    )
    return ReflectionOutcome(
        job_run_id=detail.id,
        proposal_ids=tuple(created),
        deduplicated=deduplicated,
        counters=dict(detail.counters),
    )
