# `ken_agents` — agent runtime

Two things live here:

- the Phase 0 **worker** heartbeat (`worker.py`, [ADR-0005](../../docs/adr/ADR-0005.md));
- from Phase 4, the **LangGraph supervisor** that reviews the deterministic engine's proposed
  plans ([ADR-0012](../../docs/adr/ADR-0012-agent-orchestration.md), the governing design).

## Phase 4: the supervisor

```
load_page → triage → understand → discover → plan → critique → check_invariants → policy_gate → finalize
```

The deterministic engine (`make recommend`) is the **only** producer of `proposed` plans.
An agent run reviews ONE source page's unreviewed proposed plans and hands the worthwhile ones
to the human queue (`proposed → critiqued → policy_checked → awaiting_approval`) in one service
call. Agents never insert, edit, reject, approve or expire a plan; a plan the critic rejects
stays `proposed` with its review recorded. There is no path to `approved` (AUTO_APPROVE is
unreachable).

| Module | What it is |
|---|---|
| `tracing_guard.py` | Refuses to run when LangSmith/LangChain tracing, a key or an endpoint is configured; forces tracing off before `langgraph` is imported (ADR-0012 §9). |
| `state.py` | `AgentState` (TRD §8 mapping), one partial-update model per node, `validate_update` (an undeclared key FAILS instead of being silently dropped). |
| `tools.py` | `ToolBox`: one method per `ToolName` over the frozen `AgentBackend`; timeouts, READ retries (never the hand-off), a `ToolCallRecord` per attempt. |
| `llm.py` | `RoleModel`: `complete_structured` for one role (FAST or REASONING), temperature 0, its own timeout; every model problem becomes a `FallbackReason`. |
| `prompts.py` | Short prompts; plans named by opaque refs `c0…c9` (never ids); page text sent as untrusted JSON `DATA`. |
| `nodes.py` | The nine nodes (`NodeContext` = tools, policy, planner, critic: no session, settings, provider implementation or actor). |
| `recording.py` | `StepScratch` and the deterministic 16 KiB bounds of a step's `output` and `tool_calls`. |
| `graph.py` | `build_graph` (lazy `langgraph` import) and `guarded_node`: timeout, update validation, typed failure routing, one `AgentStepView` per executed node. |
| `runner.py` | `run_one_source` (start → graph → `finish_run` exactly once, also on a crash) and `run_agent_batch` (an `agent` job run; reaps orphans; explicit sources or `sources_to_review`). |
| `cli.py` | `python -m ken_agents.cli agents [--source PAGE_ID ...]` — the composition root (wires the database-backed services lazily). |

### Models are optional

`policies/agents/agents.v1.yaml` decides which roles are used: planner (FAST) off, critic
(REASONING) on by default. Without a model, or when it is unavailable, slow or invalid, the
deterministic rule applies and is recorded (`fallback_used` + reason): engine order for the
planner; `accept` at or above `accept_min_confidence` (raw), else `uncertain`, for the critic.
A model problem never fails a run. A ref the model omits gets the rule; unknown refs are
dropped and recorded in the step output.

### Running it

```bash
make recommend                            # the engine proposes plans (Phase 3)
make agents                               # trigger "batch": sources with unreviewed proposed plans
make agents ARGS="--source <PAGE_ID>"     # trigger "manual": only these sources
```

Exit codes: 0 succeeded, 1 completed with errors / failed, 2 usage or configuration error
(including remote tracing configured), 3 another agent batch is running. Runs are inspected
via `GET /agents/runs` and `GET /agents/runs/{id}` (GET-only: no endpoint starts a run).

### Boundaries enforced by tests

`tests/unit/agents/test_boundaries.py`: no `sqlalchemy`, `httpx`, `requests`, `subprocess`,
`socket`, `langsmith`/`langchain`, Ollama provider or CMS import anywhere in the runtime (the
worker excepted, by ADR-0005), no `ken_core.db`/providers/services wiring outside `cli.py`, no
`os.system`, no `"user:"` actor, no `approved` status; `langgraph` imported only inside the
builder; the full graph runs with outbound sockets blocked.

## The worker (Phase 0)

A plain `asyncio` heartbeat tick loop. Every `KEN_WORKER_TICK_SECONDS` (default 30) it runs the
readiness probes, emits one structured `worker.heartbeat` log line and, on the first tick and
on every status change, appends one `worker.heartbeat` event. It is not a queue consumer, not
an agent and not a server; it does not import LangGraph.

```bash
.venv/bin/python -m ken_agents.worker --once   # one tick; exit 0 only if the heartbeat was written
.venv/bin/python -m ken_agents.worker          # tick loop until SIGTERM / Ctrl-C
```

In Docker Compose it is the `worker` service (profile `full`).
