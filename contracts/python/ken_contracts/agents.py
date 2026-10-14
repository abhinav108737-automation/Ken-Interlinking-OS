"""Supervised agent workflows (Phase 4, ADR-0012).

FROZEN CONTRACT (orchestrator).

One **agent run** is one supervised review workflow over ONE source page, executed by the
LangGraph supervisor (TRD sections 8-10)::

    load_page -> triage -> understand -> discover -> plan -> critique -> check_invariants
        -> policy_gate -> finalize

Boundaries this module fixes:

* **One plan producer.** The deterministic engine (``make recommend``, ADR-0011) is the ONLY
  producer of ``proposed`` plans. An agent run REVIEWS the source's existing ``proposed``
  plans: ``discover`` lists them, ``plan`` chooses which to review, ``critique`` assesses them
  and ``policy_gate`` hands the accepted and uncertain ones to the human queue
  (``proposed -> critiqued -> policy_checked -> awaiting_approval``) in ONE deterministic
  service call. Agents never insert, edit, reject, approve or expire a plan; a plan the
  critic rejects stays ``proposed`` with its review recorded.
* **Outcomes.** ``recommend`` (>= 1 accepted plan handed off), ``needs_human_review`` (only
  uncertain plans handed off, or an invariant problem), ``no_action``, ``failed``. The run
  ENDS when it hands off (status ``awaiting_approval`` = handed off; TRD section 16: no
  process waits for a human, and nothing resumes the graph).
* **LLM boundary.** A model may only CHOOSE AMONG and ASSESS plans, referred to by opaque
  refs (``c0``, ``c1`` ...). Output is one of the frozen models below, validated with
  Pydantic and then against the offered refs. A ref the model omits gets the deterministic
  rule. Anchors, offsets, URLs and scores never come from a model.
* **Deterministic fallback.** Model disabled / unavailable / slow / invalid: the node uses
  its rule and records ``fallback_used`` + ``FallbackReason``. A model problem never fails a
  run.
* **Tools.** Nodes receive narrow, typed, allowlisted tools (``ToolName``), never sessions,
  credentials, a filesystem, a shell or a CMS client, and never choose an actor.
* **Models by role.** Steps record the ROLE they asked for and the model the provider
  reported.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Final, Protocol, Self, runtime_checkable
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from ken_contracts.common import FrozenModel
from ken_contracts.errors import ListResponse
from ken_contracts.model import ModelRole

GRAPH_VERSION: Final = "graph-v1"
"""Version of the supervisor graph (nodes, edges, rules). Recorded on every run."""

MAX_EXPLANATION_CHARS: Final = 300
MAX_CANDIDATES_FOR_MODEL: Final = 10
"""At most this many plans are shown to a model in one call."""
MAX_STEP_OUTPUT_BYTES: Final = 16_384
"""Bound on the JSON of a step's ``output`` and ``tool_calls`` (also a DB CHECK)."""
MAX_REVIEW_NOTES: Final = 20
MAX_REVIEW_NOTE_CHARS: Final = 300
REF_PATTERN: Final = r"^c[0-9]{1,2}$"
"""Opaque per-call plan reference shown to a model: never a UUID or URL."""

PLANNER_ACTOR: Final = "agent:planner@graph-v1"
CRITIC_ACTOR: Final = "agent:critic@graph-v1"
POLICY_GATE_ACTOR: Final = "system:agent-policy-gate"
"""Actors are bound by the services, never chosen by a node."""

type PlanRef = Annotated[str, Field(pattern=REF_PATTERN)]


class AgentTrigger(StrEnum):
    MANUAL = "manual"
    """An operator asked for these pages (CLI): a batch of the named sources."""
    BATCH = "batch"
    """``make agents`` over sources that have unreviewed proposed plans."""


class AgentNode(StrEnum):
    """Supervisor nodes, in graph order (TRD section 9 mapping in ADR-0012)."""

    LOAD_PAGE = "load_page"
    TRIAGE = "triage"
    UNDERSTAND = "understand"
    DISCOVER = "discover"
    PLAN = "plan"
    CRITIQUE = "critique"
    CHECK_INVARIANTS = "check_invariants"
    POLICY_GATE = "policy_gate"
    FINALIZE = "finalize"


class TriageDecision(StrEnum):
    """TRD section 10 triage vocabulary. Deterministic in Phase 4."""

    NO_ACTION = "no_action"
    """Ineligible source, or no unreviewed proposed plan."""
    REINDEX = "reindex"
    """Analysis missing or stale: run ``make analyze`` (never done by an agent)."""
    OPTIMIZE = "optimize"
    """Eligible, analysed, has unreviewed proposed plans: review them."""
    REPAIR_LINK = "repair_link"
    """Reserved (Phase 6): a deployed link is broken."""
    REVIEW_PERFORMANCE = "review_performance"
    """Reserved (Phases 7-8): measured links under-perform."""


class AgentRunStatus(StrEnum):
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    """Ended by handing >= 1 plan to the human queue (terminal for the run)."""
    COMPLETED = "completed"
    """Ended without handing off a plan."""
    FAILED = "failed"


class AgentOutcome(StrEnum):
    RECOMMEND = "recommend"
    NO_ACTION = "no_action"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
    FAILED = "failed"


class StepStatus(StrEnum):
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    """The node did not apply (e.g. nothing to critique)."""
    FAILED = "failed"
    """A recoverable failure, handled by routing (the run continues or ends cleanly)."""


class FallbackReason(StrEnum):
    MODEL_DISABLED = "model_disabled"
    MODEL_UNAVAILABLE = "model_unavailable"
    MODEL_TIMEOUT = "model_timeout"
    SCHEMA_VIOLATION = "schema_violation"
    """The output did not validate, or covered none of the offered refs."""


class AgentErrorCode(StrEnum):
    """Typed, recoverable node failures (TRD section 9). Never exception text."""

    PAGE_NOT_FOUND = "page_not_found"
    TOOL_FAILED = "tool_failed"
    TIMEOUT = "timeout"
    INVARIANT_VIOLATED = "invariant_violated"
    HANDOFF_CONFLICT = "handoff_conflict"
    """A plan changed status concurrently (e.g. expired by ``make recommend``)."""
    ORPHANED = "orphaned"
    """The process died while the run was ``running`` (reaped at the next batch start)."""
    INTERNAL = "internal"


class ToolName(StrEnum):
    """The complete agent tool allowlist. Adding a tool is a governed contract change."""

    GET_PAGE = "get_page"
    GET_ANALYSIS = "get_analysis"
    GET_ELIGIBILITY = "get_eligibility"
    LIST_PROPOSED_PLANS = "list_proposed_plans"
    CHECK_PLAN = "check_plan"
    """Deterministic invariants of one plan, re-checked right before the hand-off."""
    HAND_OFF_PLANS = "hand_off_plans"
    """The ONLY writing tool: the atomic policy-gate service call (actors bound inside)."""


class ToolPermission(StrEnum):
    READ = "read"
    REVIEW = "review"
    """Moves reviewed plans proposed -> awaiting_approval and records the review. Never
    inserts, approves or deploys; never touches a CMS."""


TOOL_PERMISSIONS: Final = {
    ToolName.GET_PAGE: ToolPermission.READ,
    ToolName.GET_ANALYSIS: ToolPermission.READ,
    ToolName.GET_ELIGIBILITY: ToolPermission.READ,
    ToolName.LIST_PROPOSED_PLANS: ToolPermission.READ,
    ToolName.CHECK_PLAN: ToolPermission.READ,
    ToolName.HAND_OFF_PLANS: ToolPermission.REVIEW,
}


# --------------------------------------------------------------------------- #
# LLM boundary (validated with Pydantic, then against the offered refs)
# --------------------------------------------------------------------------- #
class PlannerSelection(FrozenModel):
    """FAST-role output: an ordered subset of the offered refs, without duplicates."""

    selected: list[PlanRef] = Field(default_factory=list, max_length=MAX_CANDIDATES_FOR_MODEL)
    explanation: str = Field(default="", max_length=MAX_EXPLANATION_CHARS)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        if len(set(self.selected)) != len(self.selected):
            raise ValueError("selected refs must be unique")
        return self


class CriticVerdict(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    """The plan is NOT handed off (it stays proposed; only a human rejects)."""
    UNCERTAIN = "uncertain"
    """Handed off with the critic's note: a human must look closely."""


class CriticReason(StrEnum):
    NATURAL_ANCHOR = "natural_anchor"
    RELEVANT_DESTINATION = "relevant_destination"
    MISLEADING_ANCHOR = "misleading_anchor"
    WEAK_RELEVANCE = "weak_relevance"
    READER_JOURNEY_MISMATCH = "reader_journey_mismatch"
    OVER_LINKING = "over_linking"
    LOW_CONFIDENCE = "low_confidence"


class CandidateCritique(FrozenModel):
    ref: PlanRef
    verdict: CriticVerdict
    reasons: list[CriticReason] = Field(default_factory=list, max_length=4)
    explanation: str = Field(default="", max_length=MAX_EXPLANATION_CHARS)


class CriticAssessment(FrozenModel):
    """REASONING-role output: at most one critique per offered ref."""

    critiques: list[CandidateCritique] = Field(max_length=MAX_CANDIDATES_FOR_MODEL)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        refs = [c.ref for c in self.critiques]
        if len(set(refs)) != len(refs):
            raise ValueError("at most one critique per ref")
        return self


# --------------------------------------------------------------------------- #
# Records and views
# --------------------------------------------------------------------------- #
class ToolCallRecord(FrozenModel):
    tool: ToolName
    permission: ToolPermission
    ok: bool
    duration_ms: int = Field(ge=0)
    attempt: int = Field(default=1, ge=1)
    error_code: AgentErrorCode | None = None
    summary: dict[str, Any] = Field(
        default_factory=dict, description="Secret-free, bounded summary of arguments/results."
    )


class AgentStepView(FrozenModel):
    run_id: UUID
    seq: int = Field(ge=0)
    node: AgentNode
    status: StepStatus
    attempt: int = Field(ge=1)
    started_at: AwareDatetime
    finished_at: AwareDatetime
    duration_ms: int = Field(ge=0)
    model_role: ModelRole | None = None
    model: str | None = Field(default=None, description="As reported by the provider.")
    fallback_used: bool = False
    fallback_reason: FallbackReason | None = None
    error_code: AgentErrorCode | None = None
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    output: dict[str, Any] = Field(
        default_factory=dict, description="Validated, bounded node output (no raw prompts)."
    )
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$", description="The run's trace id.")
    span_id: str = Field(pattern=r"^[0-9a-f]{16}$", description="This step's span.")


class AgentPlanReview(FrozenModel):
    """One plan reviewed by one run (append-only ``agent_run_plans``; a plan is reviewed
    by agents at most once)."""

    run_id: UUID = Field(description="The agent run that reviewed the plan.")
    plan_id: UUID
    verdict: CriticVerdict
    reasons: list[CriticReason] = Field(default_factory=list)
    explanation: str = Field(default="", max_length=MAX_EXPLANATION_CHARS)
    handed_off: bool = Field(description="Moved to awaiting_approval by this run.")
    fallback_used: bool
    fallback_reason: FallbackReason | None = None
    model: str | None = Field(default=None, description="Set only when a model answered.")
    reviewed_at: AwareDatetime


class AgentRunSummary(FrozenModel):
    id: UUID
    job_run_id: UUID
    source_page_id: UUID
    source_page_version: int | None = None
    source_url: str
    trigger: AgentTrigger
    status: AgentRunStatus
    outcome: AgentOutcome | None = None
    triage_decision: TriageDecision | None = None
    handed_off_plan_ids: list[UUID] = Field(default_factory=list)
    correlation_id: UUID
    trace_id: str = Field(description="``correlation_id.hex``; every step carries it.")
    graph_version: str
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    error_code: AgentErrorCode | None = None


class AgentRunDetail(AgentRunSummary):
    steps: list[AgentStepView]
    reviews: list[AgentPlanReview]
    config_snapshot: dict[str, Any] = Field(
        description="Secret-free: graph and policy versions, model roles enabled, timeouts."
    )
    review_notes: list[str] = Field(
        default_factory=list,
        max_length=MAX_REVIEW_NOTES,
        description="Why a human should look (needs_human_review).",
    )


class AgentRunNotFoundError(LookupError):
    """No agent run with that id."""


@runtime_checkable
class AgentReader(Protocol):
    async def list_runs(
        self,
        *,
        status: AgentRunStatus | None = None,
        outcome: AgentOutcome | None = None,
        source_page_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> ListResponse[AgentRunSummary]: ...

    async def get_run(self, *, run_id: UUID) -> AgentRunDetail: ...

    async def reviews_for_plan(self, *, plan_id: UUID) -> list[AgentPlanReview]:
        """The agent review(s) of a plan, for the Phase 5 reviewer (empty if none)."""
        ...


# --------------------------------------------------------------------------- #
# Policy (policies/agents/agents.v1.yaml) -- versioned data, never code constants
# --------------------------------------------------------------------------- #
class ModelUsePolicy(FrozenModel):
    use_model: bool = Field(description="False: always the deterministic rule (recorded).")
    timeout_seconds: float = Field(default=60.0, gt=0, le=600)


class CriticPolicy(ModelUsePolicy):
    accept_min_confidence: float = Field(
        default=0.5,
        ge=0,
        le=1,
        description="Deterministic rule: accept at or above this RAW confidence, else uncertain.",
    )


class AgentPolicy(FrozenModel):
    version: int = Field(ge=1)
    planner: ModelUsePolicy
    critic: CriticPolicy
    max_plans_per_run: int = Field(
        default=3,
        ge=1,
        le=MAX_CANDIDATES_FOR_MODEL,
        description="Plans reviewed per source per run; at most the engine's per-source cap.",
    )
    node_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    tool_retries: int = Field(default=1, ge=0, le=3, description="Retries of READ tools only.")
    max_sources_per_batch: int = Field(default=100, ge=1, le=10_000)
