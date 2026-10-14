"""Supervised agent workflows (Phase 4, ADR-0012).

FROZEN CONTRACT (orchestrator).

One **agent run** is one supervised workflow over ONE source page, executed by the LangGraph
supervisor (TRD sections 8-10)::

    load_page -> triage -> understand -> discover -> plan -> critique -> verify -> policy_gate
        -> finalize

Every node is recorded as one APPEND-ONLY ``agent_steps`` row. A batch of runs is one
``job_runs`` row of kind ``agent``.

Boundaries this module fixes:

* **Outcomes.** A run ends ``recommend`` (plans persisted and queued for human approval),
  ``no_action``, ``needs_human_review`` or ``failed``. Approval is never decided here: the
  run stops at ``awaiting_approval`` (TRD section 16: no live process waits for a human).
* **LLM boundary.** A model may only CHOOSE AMONG and ASSESS deterministic candidates. Its
  output is one of the models below (``PlannerSelection``, ``CriticAssessment``), validated
  with Pydantic and then re-validated against the candidate set (unknown refs are dropped and
  recorded). Anchors, offsets, URLs and scores never come from a model.
* **Deterministic fallback.** When the model is disabled, unavailable, slow or returns an
  invalid shape, the node uses its deterministic rule and records ``fallback_used`` with a
  ``FallbackReason``. A fallback never fails the run.
* **Tools.** Nodes receive narrow, typed, allowlisted tools (``ToolName``), never sessions,
  credentials, a filesystem or a shell. Every call is recorded on its step.
* **Models by role.** Steps record the ROLE they asked for and the model the provider
  reported, never a hard-coded name.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable
from uuid import UUID

from pydantic import AwareDatetime, Field

from ken_contracts.common import FrozenModel
from ken_contracts.errors import ListResponse
from ken_contracts.model import ModelRole

GRAPH_VERSION: Final = "graph-v1"
"""Version of the supervisor graph (nodes, edges, rules). Recorded on every run."""

MAX_EXPLANATION_CHARS: Final = 300
MAX_CANDIDATES_FOR_MODEL: Final = 10
"""At most this many deterministic candidates are shown to a model in one call."""


class AgentTrigger(StrEnum):
    MANUAL = "manual"
    """An operator asked for this page (CLI / API)."""
    BATCH = "batch"
    """Part of a ``make agents`` batch over eligible sources."""
    PAGE_CHANGED = "page_changed"
    """The source has a newer version than its last agent run."""


class AgentNode(StrEnum):
    """Supervisor nodes, in graph order."""

    LOAD_PAGE = "load_page"
    TRIAGE = "triage"
    UNDERSTAND = "understand"
    DISCOVER = "discover"
    PLAN = "plan"
    CRITIQUE = "critique"
    VERIFY = "verify"
    POLICY_GATE = "policy_gate"
    FINALIZE = "finalize"


class TriageDecision(StrEnum):
    """TRD section 10 triage vocabulary. Deterministic in Phase 4."""

    NO_ACTION = "no_action"
    REINDEX = "reindex"
    """The page's analysis is missing or stale: run analysis first (never done by an agent)."""
    OPTIMIZE = "optimize"
    """Eligible, analysed, has capacity: look for links."""
    REPAIR_LINK = "repair_link"
    """Reserved (Phase 6): a deployed link is broken."""
    REVIEW_PERFORMANCE = "review_performance"
    """Reserved (Phase 7/8): measured links under-perform."""


class AgentRunStatus(StrEnum):
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    """Plans are queued for a human; the run itself has ENDED (TRD section 16)."""
    COMPLETED = "completed"
    """Ended with no plan awaiting approval (no action, or review requested)."""
    FAILED = "failed"


class AgentOutcome(StrEnum):
    RECOMMEND = "recommend"
    NO_ACTION = "no_action"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
    FAILED = "failed"


class StepStatus(StrEnum):
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    """The node did not apply (e.g. no candidates to critique)."""
    FAILED = "failed"
    """A recoverable failure was handled by routing (the run continues or ends cleanly)."""


class FallbackReason(StrEnum):
    MODEL_DISABLED = "model_disabled"
    MODEL_UNAVAILABLE = "model_unavailable"
    MODEL_TIMEOUT = "model_timeout"
    SCHEMA_VIOLATION = "schema_violation"
    """The model output did not validate, or referenced no known candidate."""


class AgentErrorCode(StrEnum):
    """Typed, recoverable node failures (TRD section 9). Never exception text."""

    PAGE_NOT_FOUND = "page_not_found"
    TOOL_FAILED = "tool_failed"
    TIMEOUT = "timeout"
    INVARIANT_VIOLATED = "invariant_violated"
    """Verification found a candidate that breaks a deterministic invariant."""
    PERSIST_CONFLICT = "persist_conflict"
    """A plan could not be persisted (e.g. a concurrent run took the pair)."""
    INTERNAL = "internal"


class ToolName(StrEnum):
    """The complete agent tool allowlist. Adding a tool is a governed contract change."""

    GET_PAGE = "get_page"
    GET_ANALYSIS = "get_analysis"
    GET_ELIGIBILITY = "get_eligibility"
    DISCOVER_CANDIDATES = "discover_candidates"
    """Dry run of the deterministic engine for one source: writes nothing."""
    VERIFY_CANDIDATE = "verify_candidate"
    PROPOSE_PLANS = "propose_plans"
    """The ONLY writing tool: persists plans through the engine's insert path."""
    TRANSITION_PLAN = "transition_plan"
    """Moves a plan through the transition service, as the calling agent's actor."""


class ToolPermission(StrEnum):
    READ = "read"
    PROPOSE = "propose"
    """Writes recommendation records only (link_plans and their history). Never CMS."""


TOOL_PERMISSIONS: Final = {
    ToolName.GET_PAGE: ToolPermission.READ,
    ToolName.GET_ANALYSIS: ToolPermission.READ,
    ToolName.GET_ELIGIBILITY: ToolPermission.READ,
    ToolName.DISCOVER_CANDIDATES: ToolPermission.READ,
    ToolName.VERIFY_CANDIDATE: ToolPermission.READ,
    ToolName.PROPOSE_PLANS: ToolPermission.PROPOSE,
    ToolName.TRANSITION_PLAN: ToolPermission.PROPOSE,
}


# --------------------------------------------------------------------------- #
# LLM boundary (validated with Pydantic, then against the candidate set)
# --------------------------------------------------------------------------- #
class PlannerSelection(FrozenModel):
    """FAST-role output: an ordered subset of the offered candidate refs."""

    selected: list[str] = Field(default_factory=list, max_length=MAX_CANDIDATES_FOR_MODEL)
    explanation: str = Field(default="", max_length=MAX_EXPLANATION_CHARS)


class CriticVerdict(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    UNCERTAIN = "uncertain"
    """Routes the plan to human review with the critic's note."""


class CriticReason(StrEnum):
    NATURAL_ANCHOR = "natural_anchor"
    RELEVANT_DESTINATION = "relevant_destination"
    MISLEADING_ANCHOR = "misleading_anchor"
    WEAK_RELEVANCE = "weak_relevance"
    READER_JOURNEY_MISMATCH = "reader_journey_mismatch"
    OVER_LINKING = "over_linking"
    LOW_CONFIDENCE = "low_confidence"


class CandidateCritique(FrozenModel):
    ref: str
    verdict: CriticVerdict
    reasons: list[CriticReason] = Field(default_factory=list, max_length=4)
    explanation: str = Field(default="", max_length=MAX_EXPLANATION_CHARS)


class CriticAssessment(FrozenModel):
    """REASONING-role output: one critique per offered candidate ref."""

    critiques: list[CandidateCritique] = Field(max_length=MAX_CANDIDATES_FOR_MODEL)


# --------------------------------------------------------------------------- #
# Records and views
# --------------------------------------------------------------------------- #
class ToolCallRecord(FrozenModel):
    tool: ToolName
    permission: ToolPermission
    ok: bool
    duration_ms: int = Field(ge=0)
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
    trace_id: str = Field(description="32 hex chars: the run's trace (W3C trace-context form).")
    span_id: str = Field(description="16 hex chars: this step's span.")


class AgentRunSummary(FrozenModel):
    id: UUID
    job_run_id: UUID | None = None
    source_page_id: UUID
    source_page_version: int | None = None
    source_url: str
    trigger: AgentTrigger
    status: AgentRunStatus
    outcome: AgentOutcome | None = None
    triage_decision: TriageDecision | None = None
    plan_ids: list[UUID] = Field(default_factory=list)
    correlation_id: UUID
    trace_id: str = Field(description="32 hex chars; every step's trace_id equals it.")
    graph_version: str
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    error_code: AgentErrorCode | None = None


class AgentRunDetail(AgentRunSummary):
    steps: list[AgentStepView]
    config_snapshot: dict[str, Any] = Field(
        description="Secret-free: graph version, model roles enabled, timeouts, policy versions."
    )
    review_notes: list[str] = Field(
        default_factory=list, description="Why a human should look (needs_human_review)."
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
        default=3, ge=1, le=10, description="Never more than the engine's per-source cap."
    )
    node_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    tool_retries: int = Field(default=1, ge=0, le=3)
    max_sources_per_batch: int = Field(default=100, ge=1, le=10_000)
