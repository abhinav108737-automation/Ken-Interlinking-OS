# Shadow Pilot Performance Report

Date: 2026-10-25 · Scope: legacy-state reconciliation and critic performance · Mode: `shadow`, nothing published,
no CMS (real or mock) written, no real site crawled.

## 1. Headline

**The critic was never a two-hour bottleneck.** The shadow pilot's two-hour agent step was **host sleep**.
The pilot ran without `caffeinate`; `pmset -g log` shows the Mac asleep from 00:49 IST with a 45-second DarkWake
about every 15 minutes. Eight of the 30 agent runs each span exactly one such gap (910-977 s) while their
recorded step durations (monotonic clock, which stops during sleep) are 8-13 s. Excluding those gaps, the 30 runs
took about 267 s plus roughly 10 s each for the eight stretched runs. I wrote "about 2 hours" in the pilot report;
that was wrong and is corrected there.

| | Wall time, 30 sources |
|---|---|
| Pilot as reported (asleep) | about 2 h |
| Same production flow today, machine awake (`evals/agents/benchmark.py`, `caffeinate`) | **324 s agent batch (10.8 s per source; setup 8 s)** |
| After this pass | unchanged: **no prompt/model change met the quality bar** (section 3) |

The fix that matters is operational: `make agents` now runs under `caffeinate -i` when it exists.

## 2. Profile of the existing critic (qwen3:8b-q4_K_M, Ollama, Apple M2 16 GB)

Fixed dataset `evals/critic_perf/dataset.json`: the 48 demo-corpus plans in their production grouping (30 sources,
at most 3 per source) plus 8 planted defects (a GOOD plan whose destination is swapped for an unrelated page), 38
model calls, 64 plans. Labels are the hand-written Phase 3 judgments (written before the engine existed), not model
output. Harness: `evals/critic_perf/benchmark.py` (stdlib + httpx, loopback only, reads no database).

Production prompt, 38 calls, 386 s total:

| Stage | Time | Share | Notes |
|---|---|---|---|
| Generation (output tokens) | 332.6 s | **86%** | 4,899 output tokens (about 129 per call) at about 14.7 tok/s |
| Prompt evaluation | 49.9 s | 13% | 24,423 input tokens (max 866 per call) |
| Model load / warm-up | 0.5 s | 0.1% | 7.8 s cold start once; `keep_alive` keeps it resident |
| Prompt construction | 0.011 s | ~0% | |
| JSON parse + schema validation | 0.02 s | ~0% | 0 invalid outputs, 0 retries |
| Timeouts / fallbacks | 0 | | policy timeout 90 s; slowest call 29 s |
| Database / agent overhead | about 0.1 s per run | ~1% | other nodes: 4-37 ms each |

The bottleneck is **token generation on this hardware**, and generation scales with the free text the model writes
per candidate (explanation and reasons).

## 3. Variants, same dataset (single run each; temperature 0)

"Production-equivalent" is `baseline_prefilter`: production applies the deterministic guards AFTER the model, which
can only turn an accept into reject/no-action, so running them first yields the same final verdicts. It is the fair
quality reference. `baseline` (no guards) is shown for the raw timing.

| Variant | Wall | Speed-up vs production-equivalent | In / out tokens | GOOD accepted | ACCEPTABLE accepted | BAD accepted | Planted-bad accepted | Invalid |
|---|---|---|---|---|---|---|---|---|
| baseline (no guards) | 386 s | - | 24.4k / 4.9k | 21/21 | 16/26 | 0/1 | 0/8 | 0 |
| **baseline + guards first (production-equivalent)** | **378 s** | **1.00x** | 23.4k / 4.4k | **20/21** | 16/26 | **0/1** | **0/8** | 0 |
| compact prompt | 227 s | 1.67x | 10.8k / 3.0k | 19/21 | 16/26 | 0/1 | 0/8 | 0 |
| compact + `num_ctx` 2048 | 240 s | 1.57x | 10.8k / 3.0k | 19/21 | 16/26 | 0/1 | 0/8 | 0 |
| compact + guards first | 195 s | 1.94x | 10.2k / 2.5k | 18/21 | 15/26 | 0/1 | 0/8 | 0 |
| compact + guards, 2 / 3 concurrent | 198 s / 195 s | 1.91x / 1.94x | same | 18/21 | 15/26 | 0/1 | 0/8 | 0 |
| production prompt + "explanations < 60 chars" | 291 s | 1.30x | 24.1k / 3.2k | 19/21 | 17/26 | **1/1** | 0/8 | 0 |
| production prompt + "explanations < 30 chars" | 273 s | 1.38x | 24.1k / 2.9k | 19/21 | 17/26 | **1/1** | 0/8 | 0 |

What each lever did:

* **Shorter structured prompt (compact):** halves input tokens and cuts output by 39%; 1.67x faster. It also changed
  verdicts: it accepted 1-2 fewer GOOD plans (19/21 vs 20/21; 18/21 with guards).
* **Brevity cap on explanations:** 1.3-1.4x, but the model then **accepted the one BAD-labelled plan**, a safety
  regression in the one category that must never pass. Rejected.
* **Reduced context window:** no gain (prompts are at most 866 tokens, far under any window) and one slower
  load. Not useful.
* **Deterministic pre-filtering (guards before the model):** quality-neutral by construction, but it fired on only
  9 of 64 plans and saved about 2% of wall time (all 38 calls were still needed). Not worth changing the agent flow.
* **Repeated-context caching:** the system prompt is already a constant prefix and `keep_alive` keeps the model
  loaded; model load is 0.1% of time, so there is nothing left to cache.
* **Bounded concurrency (2 and 3):** no speed-up (195-198 s vs 195 s). The Ollama daemon serialises these requests;
  `OLLAMA_NUM_PARALLEL` is a daemon setting I did not change.
* **Smaller local model:** none installed (`ollama list` shows only `qwen3:8b-q4_K_M` and `nomic-embed-text`).
  Downloading one is an unapproved model pull, so it was not tried.

## 4. Decision

**No change to the production critic prompt, context window, concurrency or model.** The directive was to speed up
"without reducing recommendation quality or deterministic safety". Every faster variant measurably reduced the
accept rate on GOOD plans (compact: -1 to -2 of 21) and the brevity variants broke the BAD-must-never-pass check.
Quality here is judged on a 21-plan GOOD set, so a 1-plan difference is a 5-point swing and the runs are single
samples; that is a reason to keep the status quo, not evidence the faster variants are equivalent.

Shipped from this pass:

* `make agents` runs under `caffeinate -i` (the true cause of the two-hour figure).
* `evals/critic_perf/` (harness, frozen dataset, all result files) so the comparison can be repeated, for example
  against a smaller model once one is approved.
* The deterministic guards, hard blocks and the fail-closed critic mapping are untouched; `uncertain` still maps to
  NO_ACTION and nothing uncertain can publish.

## 5. Unresolved bottlenecks and options that need a decision

1. Generation speed (about 15 tokens/s for an 8B Q4 on an M2 16 GB): about 10 s per source, so roughly 3 hours for
   1,000 sources, serially. Options: a smaller or faster model (needs approval to pull and a re-run of this
   harness on labelled data), `OLLAMA_NUM_PARALLEL` on the daemon plus the harness's concurrency variants, or
   running only on plans above a deterministic score floor (changes what the critic sees; needs a quality review).
2. The compact prompt is a 1.7-1.9x option if the owner accepts about a 10% lower GOOD acceptance (a more
   conservative critic). That is a policy choice for the Ken pilot's labelled sample, not a default.
3. Quality is measured on 21 GOOD / 26 ACCEPTABLE / 1 BAD hand-written judgments over a synthetic corpus. It says
   nothing about real Ken pages.

## 6. Legacy-state resolution (`awaiting_approval`)

**Why they were stranded.** Before ADR-0018 the agent hand-off ended a reviewed plan at `awaiting_approval`
(`proposed -> critiqued -> policy_checked -> awaiting_approval`, actors `agent:critic` and
`system:agent-policy-gate`). The hand-off now ends at `policy_checked`. The autonomy engine decides only
`proposed / critiqued / policy_checked / autonomy_decided`, and `ALLOWED_TRANSITIONS` plus the database guard
(`ken_link_plan_status_guard`) have **no edge from `awaiting_approval` onto the autonomous ladder** (only
`approved`, `blocked`, `rejected`, `superseded`, `expired`). So the engine cannot decide such a plan and the plans are
not publishable except through the human approval/override path.

**What was built (no migration, no contract change, nothing published):**

1. **Shadow-only evaluation.** `SHADOW_ONLY_STATUSES = (awaiting_approval,)`. A shadow pass may evaluate and record
   a decision row for such a plan (`decidable_plans(include_shadow_only=True)`, set automatically for shadow
   passes); it moves nothing. Any publishing pass reaching one raises `ShadowOnlyStatusError` before writing.
2. **`make autonomy-legacy` (`legacy-reconcile`)**: dry run by default. Deterministic classification from the same
   facts the engine uses: `stale`, `duplicate` (an older open plan for the same pair), `protected`, `invalid`
   (any other hard block), `critic_not_accept`, `eligible`. `--apply` moves only the four defect classes, along edges
   that already exist (`awaiting_approval -> expired` for stale/duplicate, `-> blocked` for protected/invalid), by
   `system:legacy-reconcile`, one transaction per plan guarded by `expected_from`, with the class and block codes
   in the note. The original status stays in the history row's `from_status`; the transition log is append-only.
   Idempotent (a second run finds nothing in the status). It never approves, publishes, deletes or rebuilds anything.
3. **Applied to the dev database:** dry run found **33 of 33 `eligible`** (no staleness, duplicates, protections or
   blocks), so `--apply` would change nothing and was not run. I ran a shadow pass over exactly those 33 plans:
   33 decision rows recorded (25 would be `AUTO_PUBLISH_WITH_EXTRA_VERIFICATION`, 8 would be `BLOCK`), **plan statuses and
   the transition log unchanged (147 rows before and after), deployments 0, publish attempts 0.** They remain
   `awaiting_approval`; moving them further is an operator act.

**Tests** (`tests/unit/autonomy/test_legacy_classify.py`, `tests/integration/test_autonomy_legacy.py`; 12 + 7 pass):
classification table (stale beats duplicate beats protected beats invalid; non-accept never eligible); shadow
evaluation records one decision and changes neither status nor history; a guarded pass refuses the plan; dry-run by
default and eligible plans untouched; protected-page plan blocked with history `... awaiting_approval, blocked`,
`from_status = awaiting_approval`, actor `system:legacy-reconcile`, codes in the note, then idempotent; stale plan
expired; an injected failure after the transition row rolls the whole move back and a retry succeeds; a plan with
an `uncertain` review is never called eligible; no deployment, delivery or attempt rows exist afterwards.
Invalid-block and duplicate paths are covered by the classification table (duplicate also needs a second open
plan on the pair, which the demo corpus cannot produce without regenerating plans).
