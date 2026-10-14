# ADR-0012 — Phase 4 agent orchestration: a LangGraph supervisor over deterministic tools

- **Status:** Accepted (Phase 4)
- **Date:** 2026-10-14
- **Deciders:** orchestrator (under the user's 2026-10-14 autonomous-delivery authorisation)
- **Related:** TRD §§6–11, 16; ADR-0005 (worker), ADR-0011 (recommendation engine)

## Context

TRD §2 fixes LangGraph as the supervisor/state machine. TRD §11 forbids delegating any
deterministic concern (URL normalisation, hashing, duplicate detection, protections, version
checks, HTML mutation, link density, publishing) to an LLM. Phase 3 already produces natural,
verbatim, explainable plans deterministically. The question is what agents add and how they stay
safe.

## Decision

### 1. One run = one supervised workflow over one source page

```
load_page → triage → understand → discover → plan → critique → verify → policy_gate → finalize
```

- `triage` (deterministic) returns a TRD §10 `TriageDecision`: `no_action` (ineligible, or no
  capacity), `reindex` (analysis missing/stale; the run ends — agents never run analysis),
  `optimize`. `repair_link` / `review_performance` are reserved for Phases 6–8.
- `understand` loads the Phase 2 analysis (no LLM: the classification is deterministic).
- `discover` calls the engine in **dry-run** for that source: retrieval, filters, scoring and
  placement exactly as `make recommend`, writing nothing.
- `plan` may ask the FAST model to choose and order among the deterministic candidates
  (`PlannerSelection`); default rule: engine rank order.
- `critique` may ask the REASONING model to assess each planned candidate (`CriticAssessment`);
  default rule: accept at or above the policy's confidence threshold, `uncertain` below it.
  A critic can **not** reject in the plan lifecycle (only a human can); a `reject` verdict means
  the candidate is not persisted. Likewise a candidate that fails `verify` is never persisted. Automated actors
  therefore never need the terminal `rejected` status (user-only, `TRANSITION_AUTHORITY`), and
  `expired` keeps its single meaning: stale or no longer valid. Every candidate an agent drops
  is recorded on its step with the reason.
- `verify` re-checks deterministic invariants of every surviving candidate (verbatim slice,
  freshness, live eligibility and protections, open-pair uniqueness).
- `policy_gate` persists the survivors through the engine's insert path (`propose_plans`) and
  moves them `proposed → critiqued → policy_checked → awaiting_approval` with the actor classes
  of `TRANSITION_AUTHORITY`. **AUTO_APPROVE is not reachable**: the gate has no edge to
  `approved`.
- `finalize` records the outcome: `recommend` (≥ 1 accepted plan queued), `needs_human_review`
  (only `uncertain` plans, or a verification issue), `no_action`, or `failed`.

The run ENDS at `awaiting_approval` (TRD §16): no process waits for a human. Phase 5 approval
and Phase 6 deployment continue from the persisted plan state.

### 2. State, persistence and tracing — our tables, no LangGraph checkpointer

- Typed state (`AgentState`, a Pydantic model carried by the graph). LangGraph's checkpoint
  savers are not used: `langgraph-checkpoint-postgres` depends on psycopg (LGPL, and a second
  driver, ADR-0004), and a run never suspends mid-graph.
- `agent_runs` (one row per run; only status/outcome/finish columns change, enforced by a
  trigger) and append-only `agent_steps` (one row per executed node: status, attempt, timing,
  model role and reported model, fallback and reason, tool calls, bounded output, W3C-form
  `trace_id`/`span_id`). A batch is one `job_runs` row of kind `agent`.
- Correlation: one `correlation_id` per run, on every step, log line and event.

### 3. LLM boundary

- Models are reached only through `ModelProvider` by **role** (FAST, REASONING). No node
  imports Ollama or names a model.
- Output is a frozen Pydantic model (`complete_structured`), then re-validated against the
  offered candidate refs; unknown refs are dropped and recorded.
- **Deterministic fallback** on disabled / unavailable / timeout / schema violation; recorded as
  `fallback_used` + `FallbackReason`. A model problem never fails a run.
- Prompts contain only page titles, URLs, anchors and short previews of the corpus already in
  our database. No secrets. The local model only (no external API).

### 4. Tools, not infrastructure

Nodes receive a `ToolBox` of narrow, typed, allowlisted async functions (`ToolName`), each with
a `ToolPermission` (`read` / `propose`), a timeout, and a `ToolCallRecord` on its step. Nodes
never receive a session, an engine, credentials, a filesystem, a shell or a CMS client. The only
writing tools persist recommendation records through existing services; nothing touches a CMS.

### 5. Retries and timeouts

Per-node timeout and bounded retries (policy `agents.v1`) for tool failures; a node that still
fails routes to `finalize` with a typed `AgentErrorCode`. LLM calls get their own timeout and
one attempt plus the provider's schema retry.

### 6. Observability without SaaS

LangSmith is a runtime import of LangGraph. Startup refuses `LANGSMITH_TRACING`,
`LANGCHAIN_TRACING_V2`, `LANGSMITH_API_KEY` and `LANGCHAIN_API_KEY` being set, and forces
tracing off: no repository data leaves the machine.

## Consequences

- The deterministic engine stays the source of every plan; agents add selection, critique and
  explicit routing to human review, all auditable step by step.
- Without a model the system behaves exactly like Phase 3 plus a recorded deterministic critic.
- Agent evals use recorded model outputs (the mock provider's registered responses) for
  candidate selection, critic and no-action (TRD §22).

## Alternatives considered

- **LLM tool-calling agents (ReAct).** Non-deterministic control flow over our data; rejected
  for Phase 4. The graph is fixed; models fill typed slots only.
- **LangGraph Postgres checkpointer.** LGPL driver dependency and unneeded (no mid-graph
  suspension).
- **smolagents / a second framework.** TRD §2 forbids stacking frameworks without an ADR.
