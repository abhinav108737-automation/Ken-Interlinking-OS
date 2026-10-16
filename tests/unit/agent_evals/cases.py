"""Phase 4 agent evals: the recorded-fixture dataset and its ADR-0012 oracle (pure, no DB).

``tests/fixtures/agents/agent_cases.json`` holds cases over the Phase 3 demo corpus. Each case
names one SOURCE page and lists the engine's proposed plans for it (identified by
``(source URL, target URL)``, never by an id), the RECORDED model outputs (planner and/or
critic, exactly as a model would return them, using the per-call refs ``c0`` ...) and the
EXPECTED agent outcome, written from ADR-0012 before the agent ran.

This module parses the file into frozen dataclasses and provides the oracle that re-derives
outcome, status, error code, risk level, step sequence, fallback counter and the plan
partition from the per-plan expectations (double entry: the fixture states both, and
``test_agent_cases_dataset.py`` proves they agree). The DB flow lives in ``flow.py``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ValidationError

from ken_contracts.agents import (
    AgentErrorCode,
    AgentNode,
    AgentOutcome,
    AgentRunStatus,
    CriticAssessment,
    CriticReason,
    CriticVerdict,
    FallbackReason,
    PlannerSelection,
    TriageDecision,
)

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
CASES_PATH: Final = REPO_ROOT / "tests" / "fixtures" / "agents" / "agent_cases.json"
SITE: Final = "https://research.example.com"

SUITES: Final = (
    "destination-selection",
    "critic",
    "no-action",
    "malformed-output",
    "protected-content",
    "stale-page-version",
)
MODEL_KINDS: Final = ("recorded", "none", "unavailable", "timeout")
SETUP_ACTIONS: Final = ("protect", "edit", "analyse")
HOOK_POINTS: Final = ("discover", "hand_off")
HOOK_ACTIONS: Final = ("protect", "expire")
HANDED_VERDICTS: Final = frozenset({CriticVerdict.ACCEPT})
"""ADR-0019: only an accept is handed on; reject and uncertain are recorded (NO_ACTION)."""


def url(path: str) -> str:
    """Absolute canonical URL of a corpus path (the fixture stores paths)."""
    return path if path.startswith("http") else SITE + path


def recorded_text(raw: Any) -> str:
    """The model's raw answer: a string as-is, anything else as compact JSON."""
    return raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)


def schema_accepts(schema: type[BaseModel], raw: Any) -> bool:
    """Would ``complete_structured`` accept this raw answer (schema validation only)?"""
    try:
        schema.model_validate_json(recorded_text(raw))
    except ValidationError:
        return False
    return True


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class EnginePlan:
    target: str
    anchor: str
    confidence: float


@dataclass(frozen=True, slots=True)
class Action:
    action: str
    page: str | None = None
    target: str | None = None
    find: str | None = None
    replace: str | None = None


@dataclass(frozen=True, slots=True)
class Hook:
    at: str
    action: str
    page: str | None = None
    target: str | None = None


@dataclass(frozen=True, slots=True)
class ModelScript:
    kind: str
    planner: Any = None
    planner_valid: bool | None = None
    critic: Any = None
    critic_valid: bool | None = None


@dataclass(frozen=True, slots=True)
class ExpectedReview:
    target: str
    verdict: CriticVerdict
    fallback: FallbackReason | None
    reasons: tuple[CriticReason, ...] | None = None
    explanation_contains: str | None = None


@dataclass(frozen=True, slots=True)
class Expect:
    triage: TriageDecision
    outcome: AgentOutcome
    status: AgentRunStatus
    error_code: AgentErrorCode | None
    risk_level: str | None
    planner_fallback: FallbackReason | None = None
    critique_fallback: FallbackReason | None = None
    critic: tuple[ExpectedReview, ...] = ()
    invariant_problems: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    conflicts: Mapping[str, str] = field(default_factory=dict)
    handed_off: tuple[str, ...] = ()
    recorded_not_handed_off: tuple[str, ...] = ()
    unreviewed: tuple[str, ...] = ()
    unknown_refs: tuple[str, ...] | None = None
    omitted_refs: tuple[str, ...] | None = None
    notes_contain: tuple[str, ...] = ()
    prompt_contains: str | None = None


@dataclass(frozen=True, slots=True)
class Run:
    model: ModelScript
    expect: Expect
    hooks: tuple[Hook, ...] = ()


@dataclass(frozen=True, slots=True)
class Policy:
    planner: bool = False
    critic: bool = True
    max_plans_per_run: int = 3
    critic_timeout_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class Case:
    id: str
    suite: str
    group: str
    source: str
    engine_plans: tuple[EnginePlan, ...]
    policy: Policy
    setup: tuple[Action, ...]
    runs: tuple[Run, ...]
    rationale: str

    @property
    def targets(self) -> tuple[str, ...]:
        return tuple(p.target for p in self.engine_plans)

    def plan(self, target: str) -> EnginePlan:
        return next(p for p in self.engine_plans if p.target == target)

    @property
    def footprint(self) -> frozenset[str]:
        """Pages whose state decides this case: the source and its plans' targets."""
        return frozenset({self.source, *self.targets})

    @property
    def mutated_pages(self) -> frozenset[str]:
        pages = {a.page for a in self.setup if a.action in ("protect", "edit") and a.page}
        for run in self.runs:
            pages |= {h.page for h in run.hooks if h.action == "protect" and h.page}
        return frozenset(pages)


@dataclass(frozen=True, slots=True)
class Group:
    name: str
    description: str
    corpus_edits: tuple[Action, ...]


@dataclass(frozen=True, slots=True)
class Dataset:
    version: int
    threshold: float
    confidence_tolerance: float
    groups: tuple[Group, ...]
    cases: tuple[Case, ...]
    rule_baseline_group: str
    raw: Mapping[str, Any]

    def in_group(self, group: str) -> tuple[Case, ...]:
        return tuple(c for c in self.cases if c.group == group)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _opt_reason(value: Any) -> FallbackReason | None:
    return None if value is None else FallbackReason(value)


def _action(raw: Mapping[str, Any]) -> Action:
    return Action(
        action=raw["action"],
        page=raw.get("page"),
        target=raw.get("target"),
        find=raw.get("find"),
        replace=raw.get("replace"),
    )


def _review(raw: Mapping[str, Any]) -> ExpectedReview:
    reasons = raw.get("reasons")
    return ExpectedReview(
        target=raw["target"],
        verdict=CriticVerdict(raw["verdict"]),
        fallback=_opt_reason(raw.get("fallback")),
        reasons=None if reasons is None else tuple(CriticReason(r) for r in reasons),
        explanation_contains=raw.get("explanation_contains"),
    )


def _expect(raw: Mapping[str, Any]) -> Expect:
    def tup(key: str) -> tuple[str, ...]:
        return tuple(raw.get(key, ()))

    def opt_tup(key: str) -> tuple[str, ...] | None:
        return None if key not in raw else tuple(raw[key])

    return Expect(
        triage=TriageDecision(raw["triage"]),
        outcome=AgentOutcome(raw["outcome"]),
        status=AgentRunStatus(raw["status"]),
        error_code=None if raw.get("error_code") is None else AgentErrorCode(raw["error_code"]),
        risk_level=raw.get("risk_level"),
        planner_fallback=_opt_reason(raw.get("planner_fallback")),
        critique_fallback=_opt_reason(raw.get("critique_fallback")),
        critic=tuple(_review(r) for r in raw.get("critic", ())),
        invariant_problems={k: tuple(v) for k, v in raw.get("invariant_problems", {}).items()},
        conflicts=dict(raw.get("conflicts", {})),
        handed_off=tup("handed_off"),
        recorded_not_handed_off=tup("recorded_not_handed_off"),
        unreviewed=tup("unreviewed"),
        unknown_refs=opt_tup("unknown_refs"),
        omitted_refs=opt_tup("omitted_refs"),
        notes_contain=tup("notes_contain"),
        prompt_contains=raw.get("prompt_contains"),
    )


def _run(raw: Mapping[str, Any]) -> Run:
    model = raw.get("model", {"kind": "recorded"})
    return Run(
        model=ModelScript(
            kind=model["kind"],
            planner=model.get("planner"),
            planner_valid=model.get("planner_valid"),
            critic=model.get("critic"),
            critic_valid=model.get("critic_valid"),
        ),
        expect=_expect(raw["expect"]),
        hooks=tuple(
            Hook(at=h["at"], action=h["action"], page=h.get("page"), target=h.get("target"))
            for h in raw.get("hooks", ())
        ),
    )


def _case(raw: Mapping[str, Any]) -> Case:
    policy = raw.get("policy", {})
    return Case(
        id=raw["id"],
        suite=raw["suite"],
        group=raw["group"],
        source=raw["source"],
        engine_plans=tuple(
            EnginePlan(target=p["target"], anchor=p["anchor"], confidence=float(p["confidence"]))
            for p in raw["engine_plans"]
        ),
        policy=Policy(
            planner=bool(policy.get("planner", False)),
            critic=bool(policy.get("critic", True)),
            max_plans_per_run=int(policy.get("max_plans_per_run", 3)),
            critic_timeout_seconds=policy.get("critic_timeout_seconds"),
        ),
        setup=tuple(_action(a) for a in raw.get("setup", ())),
        runs=tuple(_run(r) for r in raw["runs"]),
        rationale=raw["rationale"],
    )


def parse(raw: Mapping[str, Any]) -> Dataset:
    return Dataset(
        version=int(raw["version"]),
        threshold=float(raw["accept_min_confidence"]),
        confidence_tolerance=float(raw["confidence_tolerance"]),
        groups=tuple(
            Group(
                name=name,
                description=g["description"],
                corpus_edits=tuple(_action(a) for a in g.get("corpus_edits", ())),
            )
            for name, g in raw["groups"].items()
        ),
        cases=tuple(_case(c) for c in raw["cases"]),
        rule_baseline_group=raw["rule_baseline"]["group"],
        raw=raw,
    )


@cache
def dataset() -> Dataset:
    return parse(json.loads(CASES_PATH.read_text("utf-8")))


# --------------------------------------------------------------------------- #
# Oracle (ADR-0012 section 2 rules, re-derived from the per-plan expectations)
# --------------------------------------------------------------------------- #
def rule_verdict(
    confidence: float,
    threshold: float,
    reason: FallbackReason | None = FallbackReason.MODEL_DISABLED,
) -> CriticVerdict:
    """The deterministic critic. ``model_disabled`` (policy): accept at or above the RAW
    threshold, else uncertain. ANY model problem: uncertain, always (ADR-0019: a model that
    cannot be heard never produces an accept)."""
    if reason is FallbackReason.MODEL_DISABLED and confidence >= threshold:
        return CriticVerdict.ACCEPT
    return CriticVerdict.UNCERTAIN


def rule_reasons(verdict: CriticVerdict) -> tuple[CriticReason, ...]:
    return () if verdict is CriticVerdict.ACCEPT else (CriticReason.LOW_CONFIDENCE,)


def candidates_for_run(case: Case, run_index: int) -> tuple[str, ...]:
    """Targets ``discover`` returns in run ``run_index``: engine order, minus every plan an
    EARLIER run of the case recorded a review for, minus plans an earlier hook expired."""
    gone: set[str] = set()
    for previous in case.runs[:run_index]:
        gone |= set(previous.expect.handed_off) | set(previous.expect.recorded_not_handed_off)
        gone |= {h.target for h in previous.hooks if h.action == "expire" and h.target}
    return tuple(t for t in case.targets if t not in gone)


def expected_outcome(
    expect: Expect,
) -> tuple[AgentOutcome, AgentRunStatus, AgentErrorCode | None]:
    if expect.triage is not TriageDecision.OPTIMIZE:
        return AgentOutcome.NO_ACTION, AgentRunStatus.COMPLETED, None
    verdicts = {r.target: r.verdict for r in expect.critic}
    handed = set(expect.handed_off)
    if any(verdicts.get(t) is CriticVerdict.ACCEPT for t in handed):
        return AgentOutcome.RECOMMEND, AgentRunStatus.AWAITING_APPROVAL, None
    if expect.invariant_problems or expect.conflicts:
        code = AgentErrorCode.HANDOFF_CONFLICT if expect.conflicts else None
        return AgentOutcome.NEEDS_HUMAN_REVIEW, AgentRunStatus.COMPLETED, code
    return AgentOutcome.NO_ACTION, AgentRunStatus.COMPLETED, None


def expected_risk(case: Case, expect: Expect, threshold: float) -> str | None:
    if not expect.critic:
        return None
    if expect.invariant_problems or expect.conflicts:
        return "high"
    if any(
        r.verdict is not CriticVerdict.ACCEPT or case.plan(r.target).confidence < threshold
        for r in expect.critic
    ):
        return "medium"
    return "low"


def expected_steps(expect: Expect) -> tuple[tuple[AgentNode, str], ...]:
    """(node, step status) in execution order."""
    if expect.triage is not TriageDecision.OPTIMIZE:
        return (
            (AgentNode.LOAD_PAGE, "succeeded"),
            (AgentNode.TRIAGE, "succeeded"),
            (AgentNode.FINALIZE, "succeeded"),
        )
    passed = [r for r in expect.critic if r.target not in expect.invariant_problems]
    return (
        (AgentNode.LOAD_PAGE, "succeeded"),
        (AgentNode.TRIAGE, "succeeded"),
        (AgentNode.UNDERSTAND, "succeeded"),
        (AgentNode.DISCOVER, "succeeded"),
        (AgentNode.PLAN, "succeeded"),
        (AgentNode.CRITIQUE, "succeeded"),
        (AgentNode.CHECK_INVARIANTS, "succeeded"),
        (AgentNode.POLICY_GATE, "succeeded" if passed else "skipped"),
        (AgentNode.FINALIZE, "succeeded"),
    )


def model_problem(reason: FallbackReason | None) -> bool:
    return reason is not None and reason is not FallbackReason.MODEL_DISABLED


def expected_fallback_counter(expect: Expect) -> int:
    """``AgentRunResult.fallbacks``: steps that fell back because of a MODEL PROBLEM."""
    if expect.triage is not TriageDecision.OPTIMIZE:
        return 0
    return int(model_problem(expect.planner_fallback)) + int(
        model_problem(expect.critique_fallback)
    )


def planner_called(case: Case, run: Run) -> bool:
    return (
        run.expect.triage is TriageDecision.OPTIMIZE
        and case.policy.planner
        and run.model.kind != "none"
    )


def critic_called(case: Case, run: Run) -> bool:
    return (
        run.expect.triage is TriageDecision.OPTIMIZE
        and case.policy.critic
        and run.model.kind != "none"
        and bool(run.expect.critic)
    )


def schema_for(role: str) -> type[BaseModel]:
    return PlannerSelection if role == "planner" else CriticAssessment


def oracle_problems(case: Case, ds: Dataset) -> list[str]:
    """Every way the case's stated expectations contradict ADR-0012 (empty = consistent)."""
    out: list[str] = []
    threshold = ds.threshold
    for index, run in enumerate(case.runs):
        e = run.expect
        tag = f"{case.id}[run {index}]"
        candidates = candidates_for_run(case, index)
        chosen = [r.target for r in e.critic]
        if expected_outcome(e) != (e.outcome, e.status, e.error_code):
            out.append(f"{tag}: outcome/status/error {expected_outcome(e)} by the ADR rules")
        if expected_risk(case, e, threshold) != e.risk_level:
            out.append(f"{tag}: risk {expected_risk(case, e, threshold)} by the ADR rule")
        if e.triage is not TriageDecision.OPTIMIZE:
            if chosen or e.handed_off or e.recorded_not_handed_off:
                out.append(f"{tag}: a triaged-out run reviews nothing")
            if set(e.unreviewed) != set(candidates):
                out.append(f"{tag}: every candidate stays unreviewed")
            continue
        if not candidates:
            out.append(f"{tag}: optimize needs at least one candidate")
        if len(chosen) > case.policy.max_plans_per_run or len(set(chosen)) != len(chosen):
            out.append(f"{tag}: at most max_plans_per_run distinct plans are chosen")
        if not set(chosen) <= set(candidates):
            out.append(f"{tag}: the chosen plans must be candidates")
        if not case.policy.planner and chosen != list(candidates[: case.policy.max_plans_per_run]):
            out.append(f"{tag}: with the planner off the rule is engine order, capped")
        if not case.policy.planner and e.planner_fallback is not FallbackReason.MODEL_DISABLED:
            out.append(f"{tag}: planner off -> plan step falls back model_disabled")
        for review in e.critic:
            plan = case.plan(review.target)
            if review.fallback is not None:
                if review.verdict is not rule_verdict(plan.confidence, threshold, review.fallback):
                    out.append(f"{tag}: {review.target} fallback must use the rule")
            elif not case.policy.critic or run.model.kind != "recorded":
                out.append(f"{tag}: {review.target} cannot have a model verdict")
        if not case.policy.critic and (
            e.critique_fallback is not FallbackReason.MODEL_DISABLED
            or any(r.fallback is not FallbackReason.MODEL_DISABLED for r in e.critic)
        ):
            out.append(f"{tag}: critic off -> every review is the rule, model_disabled")
        blocked = set(e.invariant_problems) | set(e.conflicts)
        if set(e.invariant_problems) & set(e.conflicts):
            out.append(f"{tag}: a plan that fails check_invariants never reaches the gate")
        verdict = {r.target: r.verdict for r in e.critic}
        handed = {t for t in chosen if verdict[t] in HANDED_VERDICTS and t not in blocked}
        recorded = {t for t in chosen if verdict[t] not in HANDED_VERDICTS and t not in blocked}
        if set(e.handed_off) != handed:
            out.append(f"{tag}: handed_off must be {sorted(handed)}")
        if set(e.recorded_not_handed_off) != recorded:
            out.append(f"{tag}: recorded_not_handed_off must be {sorted(recorded)}")
        unreviewed = set(candidates) - handed - recorded
        if set(e.unreviewed) != unreviewed:
            out.append(f"{tag}: unreviewed must be {sorted(unreviewed)}")
        if not blocked <= set(chosen):
            out.append(f"{tag}: only chosen plans can fail checks or conflict")
    return out


def validate_text_answers(case: Case) -> list[str]:
    """Recorded raw answers must validate (or not) exactly as the case declares."""
    out: list[str] = []
    for index, run in enumerate(case.runs):
        for role, raw, valid in (
            ("planner", run.model.planner, run.model.planner_valid),
            ("critic", run.model.critic, run.model.critic_valid),
        ):
            if raw is None:
                continue
            if valid is None:
                out.append(f"{case.id}[run {index}]: {role}_valid must be declared")
            elif schema_accepts(schema_for(role), raw) is not valid:
                out.append(f"{case.id}[run {index}]: {role} answer validity is not {valid}")
    return out


def unique(values: Sequence[str]) -> bool:
    return len(set(values)) == len(values)
