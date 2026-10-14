# Phase 4 Report — Agent Orchestration

**Status: complete.** Executed under the user's 2026-10-14 authorisation for autonomous sequential
delivery of Phases 2–9. Each phase has internal gates, a local commit and a `phase-N-complete` tag.
Nothing has been pushed.

**Design:** [docs/adr/ADR-0012-agent-orchestration.md](docs/adr/ADR-0012-agent-orchestration.md).
**Evals:** [evals/agents/README.md](evals/agents/README.md).

## 1. What was built

A **LangGraph supervisor that reviews** the deterministic engine's proposals, one source page per
run. Agents add selection, critique and explicit routing to human review. They never create,
approve, reject, expire or publish a link.

```
load_page → triage → understand → discover → plan → critique → check_invariants → policy_gate → finalize
```

| Capability | Implementation |
|---|---|
| **Graph** | A fixed `StateGraph`. Each node is wrapped by a guard that validates its return against a typed update model; an undeclared key fails, so LangGraph cannot drop it silently. The final state is re-validated. A typed error from any node routes to `finalize`. |
| **Triage** (deterministic) | TRD §10 vocabulary: `no_action` (gone or ineligible, or nothing to review), `reindex` (analysis missing or stale; agents never run analysis), `optimize`. `repair_link` and `review_performance` are reserved. |
| **Model slots** | FAST planner (off by default) and REASONING critic (on), reached by role through `ModelProvider`. Plans are shown as opaque refs `c0…`, never ids. Page text goes in as a DATA JSON value and is declared untrusted. Output is validated by strict frozen models (`PlannerSelection`, `CriticAssessment`): unique refs, bounded text, no control characters. Unknown refs are dropped; omitted refs get the deterministic rule. |
| **Deterministic fallback** | Model disabled, unavailable, timed out or invalid → the rule (accept at raw confidence ≥ 0.5, else uncertain), recorded with a `FallbackReason`. A model problem never fails a run. |
| **Invariants** | `check_invariants`, then a re-check under each plan's row lock inside the hand-off. A plan must still be `proposed`, fresh or rebasable, with its anchor verbatim, both pages eligible, and the pair not already linked. |
| **Hand-off** | One transaction with a savepoint per plan. Accept and uncertain plans move `proposed → critiqued → policy_checked → awaiting_approval`, with the actors (`agent:critic@graph-v1`, `agent:policy-gate@graph-v1`) bound inside the service. Every review goes to append-only `agent_run_plans`, so a plan is reviewed once. Conflicts (`not_proposed`, `already_reviewed`, or an invariant code) write nothing for that plan. |
| **Tools** | A `ToolBox` over the frozen `AgentBackend` protocol: five READ tools (timeout, bounded retries) and one REVIEW tool (never retried, no asyncio deadline). Nodes receive no session, credentials, filesystem, shell, CMS client or actor. |
| **Persistence** | Migration `0005_agent_runs`. `agent_runs` belongs to a `job_runs` batch; finished rows are frozen and lifecycle CHECKs apply. One `running` run per source; `trace_id = correlation_id.hex`. `agent_steps` is append-only (per node: timing, model role and reported model, fallback, tool calls, bounded output, span id). Orphan reaping runs at each batch start. |
| **No egress** | Any non-falsy `LANGSMITH_*` / `LANGCHAIN_*` variable refuses the run. Every tracing flag is forced to `"false"` and LangSmith's cache is cleared. Tests cover a blocked-socket full run and an audit-hook run (no process, socket or file write). |
| **API** (GET-only) | `/agents/runs`, `/agents/runs/{id}`, `/agents/plans/{plan_id}/reviews`. No endpoint starts a run. |
| **UI** | `/agents` (run history, filters), `/agents/[runId]` (header, ids, config, review notes, reviews, step timeline with tool calls and fallbacks), and an "Agent review" panel on every plan. Model text is rendered as text only. |
| **CLI** | `make agents` (batch) and `make agents ARGS="--source <id> ..."` (manual batch). |

## 2. Schema — migration `0005_agent_runs`

Tables: `agent_runs`, `agent_steps`, `agent_run_plans`. Constraints:
- every enum CHECK;
- status, outcome and error code consistency: failed iff failed, recommend ⇒ handed off,
  no_action ⇒ completed, failed ⇒ code;
- `trace_id` equals the `correlation_id` hex, and `correlation_id` is unique;
- step payloads ≤ 16 KiB, and `model` implies `model_role`;
- a reject is never handed off, and `plan_id` is unique;
- RESTRICT foreign keys.

Triggers: `ken_agent_run_guard()` (DELETE refused; finished rows frozen; only the listed columns
change) and `ken_reject_mutation()` on steps and reviews. The round trip 0005 → 0004 → head is
clean.

## 3. Results

**Live, on the dev DB** (`make agents`, local `qwen3:8b-q4_K_M` critic, synthetic demo corpus):

| Measure | Value |
|---|---|
| Sources reviewed | 30, in one `agent` batch (`succeeded`) |
| Runs | 26 `recommend` / `awaiting_approval`, 4 `no_action`, 0 failed |
| Plans reviewed | 48 |
| Handed off | 33 accepted |
| Rejected | 15 (they stay `proposed`) |
| Fallbacks / schema violations | 0 / 0 |
| Critique latency | about 11 s mean (24.8 s max) |

The rejections looked sensible on inspection: they flagged cross-country links (Thailand
logistics → Qatar logistics) and weak topical fits.

**Evals** (`evals/agents/`):
- **Recorded cases:** 48 cases (51 runs) match exactly.
- **Hard gates:** 9 gates, 0 violations. A self-test shows each gate fires on bad records.
- **Model off:** with the critic model disabled, results equal the deterministic rule on all 48
  plans.
- **Benchmark:** the live qwen3 critic agrees with the Phase 3 judgments on 0.79 of plans,
  accepts 0.95 of GOOD plans and rejected the one BAD plan. Verdicts were identical across two
  runs.

## 4. Dependencies

- `langgraph==1.2.12` and its pinned set (`langchain-core`, `langgraph-checkpoint` as a library
  only, `langgraph-prebuilt`, `langgraph-sdk`, `langsmith`, `httpx2`/`httpcore2`, `orjson`,
  `ormsgpack`, `xxhash`, `zstandard`, `requests`, …). All are MIT, BSD or Apache; `orjson` is
  "MPL-2.0 AND (Apache-2.0 OR MIT)", used unmodified.
- `websockets` is downgraded 17.2 → 16.1.1, because `langgraph-sdk` caps it below 17.
- No checkpointer: the Postgres saver needs psycopg, which is LGPL. No paid or remote
  dependency; `pip check` is clean.

## 5. Verification (final run, on the final code)

| Gate | Result |
|---|---|
| `make verify` | **exit 0**: ruff clean, mypy clean (245 files; strict now covers `ken_agents`), **2,755** Python tests passed, **769** web tests passed, compose ×3 OK, migrations at head, doctor **0 FAIL / 0 WARN** |
| Agent evals | 48 recorded cases plus the gates, run inside the full suite |
| OpenAPI drift | regenerated; snapshot test green |
| Electron guard | lint clean; no Electron import; text-only rendering |
| Live batch | above (§3) |

## 6. Reviews

**Pre-fan-out architecture review: approved with required changes.** The main change was the
coexistence decision. The first draft let agents insert plans from an engine dry run, which
gave two producers, insert races, cap overshoot and a critic bypass. It was replaced by
"agents review engine plans". The review also brought in:
- `agent_run_plans`, and `job_run_id` NOT NULL;
- strict boundary models;
- the state guard, lifecycle CHECKs and orphan handling;
- the TRD §8, §9 and §16 mappings;
- size bounds, a static boundary test and the policy cross-check.

All were applied before fan-out (commit `947c1a5`).

**Integration decisions.** I accepted three backend-engineer proposals:
- invariants re-checked under lock at hand-off;
- an `already_reviewed` conflict code;
- `append_step` requires a running run.

I also added `run_id` to `AgentPlanReview`, so a plan's review links to its run.

**QA/security review: blockers found, all fixed.**

| Finding | Fix |
|---|---|
| **Critical C1.** `LANGSMITH_TRACING_V2=true` bypassed the tracing guard: LangSmith reads it before `LANGCHAIN_TRACING_V2`. The probe ran with sockets blocked, so nothing left the machine. | Namespace-wide, fail-closed refusal of any non-falsy `LANGSMITH_*` / `LANGCHAIN_*` variable; every tracing flag overwritten to `"false"`; LangSmith's cache cleared. Regression tests per name and for the cache. |
| **High H1.** A NUL in model text failed the run at the database, on every batch. | `ModelText` rejects control characters, so the answer becomes a schema-violation fallback. A backstop in the backend. A database-backed test of both the planner and the critic, mutation-checked. |
| **High H2.** A hard-crashed run was never reaped, blocking its source forever. | Each batch, holding the per-kind lock, reaps every `running` run of any other batch. Integration test. |
| Medium M1. The static boundary test could be bypassed. | Forbidden `importlib`, `ctypes`, `shutil`, `os` (outside the guard) and similar; attribute-name and builtin-call checks; a process-wide audit-hook runtime test, plus a test proving the hook is not vacuous. QA's bypass file is now caught. |
| Low L1. A hand-off that had committed could be reported as failed after a timeout. | No asyncio deadline on the write tool or the `policy_gate` node. Test. |
| Low L2. Lock-order deadlock risk. | All plan locks taken up front in id order. |
| Low L3. The `system:` gate actor was broader than needed. | Now `agent:policy-gate@graph-v1` (least privilege). |
| Low (evals). A misleading conflict note. | Wording distinguishes a concurrent change from a failed re-check. |

**QA re-verification: PASS WITH ONE REQUIRED FIX, applied.** Every item above was re-verified by
probe:
- C1: `LANGSMITH_TRACING_V2`, `LANGCHAIN_OTEL_ONLY`, `LANGSMITH_TRACING_MODE` and
  `LANGSMITH_RUNS_ENDPOINTS` are each refused, with 0 connection attempts;
- H1: NUL in raw model JSON becomes a fallback for both the planner and the critic;
- H2: a simulated hard crash is reaped and its source reviewed;
- M1: the bypass file is caught.

The L1 fix had removed the asyncio deadline, which made the hand-off unbounded on a held plan
lock (**N1**). It is now bounded by the database (`SET LOCAL lock_timeout` / `statement_timeout`
in the hand-off transaction). The abort is server-side and comes before commit, and an
integration test covers it. QA's residual informational gap, `getattr` with a string-built
name, is closed by forbidding dynamic attribute names outside the state guard and the
`builtins` import; QA's second bypass file is now caught.

## 7. Incidents

None. Every specialist used its own test database. QA's tracing probe ran with sockets blocked.
No environment used for real runs had a tracing variable set.

## 8. Known gaps

See NEXT_STEPS.md ("Phase 4 known limitations"):
- a plan that fails invariants is reviewed again later;
- critique latency is about 10 s per source;
- verdicts and the threshold are uncalibrated (Phase 8);
- fixture drift if rankings change;
- the websockets cap;
- runs start from the CLI only, with an authenticated trigger a Phase 5 decision.

## 9. Information still needed from you

- **New decision:** should a model critic's `reject` filter a plan out of the human queue, or
  only annotate it? The default is to filter: the plan stays `proposed` with its reasons. On
  the demo, qwen3 filtered 23 % of plans, including one GOOD plan.
- Still open from Phase 3: the self-describing-anchor rule (ADR-0011 §9.1).
- Everything in NEXT_STEPS "Needed from you" still applies.

## 10. Commit

`feat: complete Phase 4 Agent Orchestration`, tag `phase-4-complete` (local only).
