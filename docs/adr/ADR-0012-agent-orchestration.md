# ADR-0012 — Phase 4 agent orchestration: a LangGraph supervisor that reviews deterministic plans

- **Status:** Accepted (Phase 4); amended before fan-out by the pre-fan-out architecture review
- **Date:** 2026-10-14
- **Deciders:** orchestrator (under the user's 2026-10-14 autonomous-delivery authorisation)
- **Related:** TRD §§2, 6–11, 16, 19, 22; ADR-0005 (worker), ADR-0011 (recommendation engine)

## Context

TRD §2 fixes LangGraph as the supervisor and state machine. TRD §11 forbids delegating any
deterministic concern to an LLM, including URL normalisation, hashing, duplicate detection,
protections, version checks, HTML mutation, link density and publishing. Phase 3 already
produces natural, verbatim, explainable plans deterministically. The question is what agents
add, and how they stay safe.

## Decision

### 1. One plan producer; agents REVIEW

The deterministic engine (`make recommend`, ADR-0011) is the **only** producer of `proposed`
plans. An agent run reviews one source page's unreviewed `proposed` plans and hands the ones
worth a human's time to the review queue. The human queue (Phase 5) shows `awaiting_approval`.

This settles the engine/agent coexistence question raised by the review:
- no second insert path;
- no insert race between batches, and no cap overshoot;
- no critic bypass. A plan the critic rejects stays `proposed` with its review recorded, and a
  re-run of the engine never re-inserts its pair, because it is still open.

**Agents never insert, edit, reject, approve or expire a plan.** (Amended by ADR-0013: the
Phase 5 review service also inserts the superseding plan of a HUMAN edit; agents still insert
nothing.)

### 2. The graph

```
load_page → triage → understand → discover → plan → critique → check_invariants → policy_gate → finalize
```

| Node | What it does | Model |
|---|---|---|
| `load_page` | Loads the source snapshot. Missing page → `failed` (`page_not_found`). | — |
| `triage` | Returns a TRD §10 decision. `reindex`: analysis missing or stale, so the run ends; agents never run analysis. `no_action`: source ineligible, or no unreviewed proposed plan. `optimize`: review. `repair_link` and `review_performance` are reserved for Phases 6–8. | — |
| `understand` | Loads the Phase 2 classification. | — |
| `discover` | Loads the source's unreviewed `proposed` plans, in engine order. | — |
| `plan` | Chooses which plans to review and in what order, at most `max_plans_per_run`. Rule: engine order. | FAST, optional (`PlannerSelection`) |
| `critique` | Assesses each chosen plan: `accept`, `reject` or `uncertain`, with reasons. Rule: `accept` if raw confidence ≥ `accept_min_confidence`, else `uncertain`. | REASONING, optional (`CriticAssessment`) |
| `check_invariants` | Re-checks each plan against live state: still `proposed`; source version unchanged or rebasable; anchor verbatim; source and target eligible; not already linked. A failing plan is not handed off and adds a review note. | — |
| `policy_gate` | One `hand_off` service call, one transaction. It records every review in `agent_run_plans`. Each `accept` or `uncertain` plan that passed its checks moves `proposed → critiqued` (`agent:critic@graph-v1`) `→ policy_checked → awaiting_approval` (`agent:policy-gate@graph-v1`). | — |
| `finalize` | Outcome and status (below). | — |

**Outcome rules:**
- `recommend` and status `awaiting_approval` when ≥ 1 accepted plan was handed off;
- `needs_human_review` when only uncertain plans were handed off (status `awaiting_approval`),
  or when an invariant problem or conflict was found and nothing was handed off (status
  `completed`);
- `no_action` (status `completed`);
- `failed` (status `failed`, with an `AgentErrorCode`).

**AUTO_APPROVE is unreachable.** The policy gate has no edge to `approved`:
`TRANSITION_AUTHORITY` reserves approval for `user:` actors, and the gate's actors are bound
inside the service.

### 3. TRD mappings

**TRD §9.** The state machine maps as follows:
- LOAD_PAGE → `load_page`;
- TRIAGE → `triage`;
- UNDERSTAND → `understand`;
- RETRIEVE, FILTER, RANK and PLACE → the deterministic engine run (`make recommend`), whose
  `proposed` plans `discover` loads;
- CRITIQUE → `plan` + `critique`;
- POLICY_GATE → `check_invariants` + `policy_gate`;
- HUMAN_APPROVAL → Phase 5;
- VALIDATE, PUBLISH and VERIFY → Phase 6 services;
- MEASURE → Phase 7.

**TRD §16** (persist AWAITING_APPROVAL; resume from an approval event). The **plan** is
persisted as `awaiting_approval` and is the durable unit of work. The **run** ends when it hands
off: its `awaiting_approval` status is terminal and means "handed off". No process waits for a
human and no event resumes the graph. Phases 5 and 6 continue from the plan's state, through
deterministic services.

**TRD §8 state.** `AgentState` is a Pydantic model owned by `ken_agents`. Its fields map:

| TRD §8 field | Phase 4 meaning |
|---|---|
| `task_id` | `run_id` |
| `source_page_id` | `source_page_id` |
| `trigger` | `trigger` |
| `source_page` | `source_page`: the snapshot |
| `classification` | the Phase 2 analysis |
| `candidates` | the unreviewed proposed plans |
| `ranked_candidates` | the planner's selection |
| `link_plans` | the plans handed off |
| `critic_result` | per-plan reviews |
| `risk_level` | a deterministic `low`/`medium`/`high` from confidence and invariant problems |
| `approval_required` | always `True` in Phase 4, with the reason "AUTO_APPROVE disabled (TRD §20)" |
| `errors` | `errors` |
| `approval_result`, `deployment_result`, `verification_result` | not applicable until Phases 5–6 |

### 4. State integrity

- Each node returns a typed partial-update model, dumped with `exclude_unset`. LangGraph would
  otherwise silently drop an undeclared key. The final result is re-validated with
  `AgentState.model_validate`.
- A node that returns an undeclared key fails, and a test proves it.

### 5. Persistence and tracing — our tables, no LangGraph checkpointer

- **Every run belongs to a `job_runs` batch** of kind `agent`; a manual run is a batch of one.
  The batch provides the advisory lock and `job.completed`.
- **`agent_runs`:**
  - only the listed columns change, and only while `running`; finished rows are frozen and
    DELETE is rejected (trigger);
  - lifecycle CHECKs keep status, outcome and error code consistent;
  - at most one `running` run per source;
  - `trace_id = correlation_id.hex`, and `correlation_id` is unique;
  - each batch start reaps orphaned `running` rows, those whose batch has finished, as
    `failed`/`orphaned`.
- **`agent_steps`** is append-only, one row per executed node. Each row holds status, attempt,
  timing, the model role and the model the provider reported, fallback and reason, tool calls,
  bounded output (≤ 16 KiB, DB CHECK) and `span_id`.
- **`agent_run_plans`** is append-only, one row per reviewed plan. It holds the verdict,
  reasons, explanation, `handed_off`, the fallback, and the model only when a model answered.
  `plan_id` is UNIQUE, so a plan is reviewed by agents once and re-runs never re-spend a model
  call on it. The Phase 5 reviewer reads the critic's note from here.
- **No checkpointer.** `langgraph-checkpoint-postgres` depends on psycopg (LGPL, and a second
  driver, ADR-0004), and a run never suspends mid-graph.

### 6. LLM boundary

- Models are reached only through `ModelProvider`, by **role** (FAST, REASONING). No node
  imports a provider implementation or names a model.
- Plans are shown to a model under opaque per-call refs (`c0`…`c9`), never ids or URLs.
- Output is a frozen Pydantic model, validated by `complete_structured` with unique refs and
  bounded text. It is then re-validated against the offered refs: unknown refs are dropped and
  recorded, and an omitted ref gets the deterministic rule.
- **Deterministic fallback** applies when the model is disabled, unavailable, times out or
  violates the schema. It is recorded as `fallback_used` + `FallbackReason`, and a model problem
  never fails a run.
- **Corpus text is untrusted input.** A prompt-injected page can at worst make the critic drop
  a plan or accept one, and a human still approves every plan. Critic explanations are rendered
  as plain text.
- Only the local model is used; there is no external API.

### 7. Tools, not infrastructure

- Nodes receive a `ToolBox` over the frozen `AgentBackend` protocol
  (`ken_services/agents/protocol.py`). It has five READ tools and one REVIEW tool
  (`hand_off_plans`), each with a timeout and a `ToolCallRecord` on its step.
- READ tools are retried (`tool_retries`); the REVIEW tool is not.
- Nodes never receive a session, an engine, credentials, a filesystem, a shell, a CMS client or
  an actor string.
- A static test enforces that `ken_agents` does not import SQLAlchemy sessions or engines, a
  provider implementation, httpx, subprocess or `os.system`, and does not contain `"user:"`.

### 8. Retries, timeouts, failure

- Each node has a timeout (`node_timeout_seconds`) and each model call its own. READ tools get
  bounded retries.
- The write path has no asyncio deadline: neither the `hand_off_plans` tool nor the
  `policy_gate` node. A cancellation that landed after the hand-off committed would report a
  failed run whose plans were already queued. The transaction is bounded by the **database**
  instead: `SET LOCAL lock_timeout` (5 s) and `statement_timeout` (30 s). A held plan lock
  aborts it server-side BEFORE commit, giving a clean rollback with nothing written and a
  `tool_failed` run (QA N1).
- Model text may not contain control characters (`ModelText`). PostgreSQL rejects NUL, so
  such an answer is a recorded schema-violation fallback, never a failed run.
- A batch reaps every `running` run that belongs to another batch. It holds the per-kind
  advisory lock, so that other batch is gone, even after a hard crash left its `job_runs` row
  `running`.
- A node that still fails routes to `finalize` with a typed `AgentErrorCode`. A concurrent
  status change found at hand-off (`make recommend` expired the plan) is reported per plan.
  When nothing was handed off, the run's code is `handoff_conflict`.

### 9. No data leaves the machine

Importing `langgraph.graph` loads `langsmith`, `langgraph_sdk`, `requests`, `httpx2` and
`websockets`. LangSmith resolves every setting through `get_env_var(name)`, which tries
`LANGSMITH_<name>` and then `LANGCHAIN_<name>`. A name-by-name refusal list therefore always
misses one. The QA review found `LANGSMITH_TRACING_V2`, which LangSmith reads before the
`LANGCHAIN_TRACING_V2` that was being forced off.

The rule is therefore **fail-closed by namespace**:
- an agent run refuses to start while ANY `LANGSMITH_*` or `LANGCHAIN_*` variable has a
  non-falsy value; the refusal message names the variables, never their values;
- once that check passes, every tracing flag of both namespaces (`TRACING`, `TRACING_V2`,
  `OTEL_ENABLED`, `OTEL_ONLY`) is overwritten with `"false"`;
- LangSmith's cached environment lookups are cleared, before `langgraph` is first imported.

Tests cover each name, the cache, a fresh process, the full graph with outbound sockets
blocked, and a process-wide audit hook. The hook asserts that a graph run spawns no process,
opens no socket and writes no file.

### 10. Policies

`policies/agents/agents.v1.yaml` (`AgentPolicy`) holds which model roles are used, the
deterministic critic threshold, `max_plans_per_run`, timeouts and retries. At load it is
cross-checked: `max_plans_per_run` must be ≤ the recommendation policy's
`max_plans_per_source`. The agents, recommendation and ranking policy versions and the graph
version are recorded in every run's `config_snapshot`.

### 11. API and UI

The Phase 4 agents API is **GET-only**: `/agents/runs` and `/agents/runs/{id}`. No
unauthenticated endpoint starts a run; runs start from the CLI (`make agents`). The UI shows
run history, the step timeline with tool calls and fallbacks, and each plan's review.

## Dependencies

All are permissive and carry no paid or remote dependency:
- `langgraph==1.2.12`, with `langchain-core`, `langgraph-checkpoint` (library only, no saver
  used), `langgraph-prebuilt`, `langgraph-sdk` and `langsmith`, all MIT;
- `httpx2` / `httpcore2` (BSD-3), a second HTTP stack pulled in only by `langsmith` and never
  used for outbound calls (§9);
- `orjson`, "MPL-2.0 AND (Apache-2.0 OR MIT)". MPL is file-level copyleft and is used
  unmodified, which is acceptable; this is recorded here;
- `websockets` is capped below 17 by `langgraph-sdk`, so it is downgraded from 17.2 to 16.1.1.
  That still satisfies `uvicorn[standard]` and `langsmith`, and the project does not use
  websockets directly.

## Consequences

- The deterministic engine stays the source of every plan. Agents add selection, critique and
  explicit routing to human review, all auditable step by step.
- Without a model the system behaves like Phase 3 plus a recorded deterministic critic.
- Agent evals use recorded model outputs (the mock provider's registered responses) for
  candidate selection, critic and no-action, plus the TRD §19 protected-content and
  stale-page-version cases for `check_invariants`.
- A critic-rejected plan stays `proposed`. The Phase 5 reviewer can still see it, with the
  critic's reasons, but it is not queued.

## Alternatives considered

- **Agents insert plans from an engine dry run** (the first draft). Rejected by the review: it
  creates two producers, insert races, cap overshoot and a critic bypass.
- **LLM tool-calling agents (ReAct).** They give non-deterministic control flow over our data.
  The graph is fixed; models fill typed slots only.
- **LangGraph Postgres checkpointer.** It needs an LGPL driver and is unneeded, because no run
  suspends mid-graph.
- **smolagents or a second framework.** TRD §2 forbids stacking frameworks without an ADR.
