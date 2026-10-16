# ADR-0016 — Phase 8 Learning: eval datasets, an offline eval runner, the skill and policy lifecycles, experiments

- **Status:** Accepted, 2026-10-16. **§15 holds the binding amendments from the pre-fan-out architecture review and OVERRIDES §1–§14 wherever they conflict; §16 records the orchestrator's decisions on the open items.** Supersedes nothing; **owes** ADR-0015 §12.8 (experiments deferred to Phase 8) and the Phase 0 backlog item "version-aware event reads (cleanup QA M1)".
- **Related:** PRD §§8, 9, 10; TRD §§5, 15, 17, 18, 19, 22; CLAUDE.md "Learning Design Rule"; AUTONOMY_AND_PERMISSIONS.md; ADR-0011 (plans, ranking policy, judgments), ADR-0012 (agents REVIEW only; `agent:*` actors never insert/approve/reject/expire), ADR-0013 (RBAC, structured reasons), ADR-0015 §12 (binding).

## 1. Context the code already gives us

`JobKind.EVALUATION` exists. `EventName.SKILL_PROPOSED / SKILL_PROMOTED / POLICY_PROPOSED / POLICY_PROMOTED` exist, and `EventEnvelope` already refuses a `*.promoted` event without a human `promoted_by` and an `evaluation_ref` (re-validated in `append_event`). `LinkPlanStatus.MEASURED → LEARNING_CAPTURED` is already in `ALLOWED_TRANSITIONS` and `TRANSITION_AUTHORITY` (`system:` only). `link_measurements` makes a past measurement reproducible. `review_actions` is append-only with `REASONS_BY_ACTION`-constrained reasons; `agent_run_plans` holds every agent verdict. `ExperimentGuard` is a frozen protocol with `NoExperiments` injected at `pipeline.py:891` and `review/service.py:316`. Policies are versioned YAML served with a sha256 by `/settings/policies`. **Nothing new is needed in the event vocabulary and no new job kind is needed except one.**

## 2. Decision 1 — what is learned, and from what

Three streams, three trust tiers. This is the spine of the phase.

| Stream | Source tables | Tier | May it gate a promotion? |
|---|---|---|---|
| **Editorial** | `review_actions` (action, reasons, note, anchor_before/after, target_before/after), `link_plan_transitions`, `agent_run_plans` | **Trustworthy.** Authenticated human actors, append-only, structured reasons, no spoofing surface. | **Yes — the only stream that can.** |
| **Behavioural** | `link_metrics_daily`, `link_events`, `link_measurements` | **Advisory.** First-party counts are spoofable (Origin and `pv` are attacker-chosen, ADR-0015 §12.3); GA4 has `(other)` rows, thresholding and sampling; `overlapping_changes` flags most batch deployments. | **No. It may only BLOCK**: a negative behavioural signal fails a gate; a positive one never passes one. |
| **Search** | `page_metrics_daily` (search_console), plus the Phase-7-deferred `page_query_metrics_daily` | **Diagnostic only.** Confounded by seasonality, Google updates and sitewide trends, with no counterfactual. | **No, ever.** It surfaces hypotheses for a human. |

Derived learning features all come from the plan join that already exists: `relationship_type`, `anchor_source`, `reason_codes`, `channels`, score components, and the five version stamps.

**What must never be learned automatically** (the FROZEN FIELDS, refused at the contract boundary, not merely at review): everything under `recommendation.placement` (`excluded_ancestors`, `block_kinds`, `forbidden_anchors`, anchor length bounds); `max_internal_links_per_100_words` and `max_links_per_block` except downward; `rejection_cooldown_days` except upward; `excluded_target_paths` except by growing; all protected-page rules in `taxonomy`; the whole `analytics` policy (measurement must never be tuned by the thing it measures); any `Settings` value, any model name, AUTO_APPROVE, and any autonomy level (Phase 9).

## 3. Decision 2 — reflection → proposal

**Reflection is a deterministic job, not an agent-graph node.** The graph is per-source-page and terminates at hand-off (ADR-0012); reflection is corpus-level. New `JobKind.REFLECTION = "reflection"` (additive, one per phase per the contract docstring), `make reflect`.

`ken_services/learning/aggregate.py` computes **every number** in pure SQL/Python over the three streams: per decision class (relationship type × anchor_source × channel set) `sample_count`, `approval_rate`, `edit_rate`, `reject_rate`, reason histograms, anchor-edit character deltas, target-replacement pairs, rollback rate, and — behaviourally — per-class `MetricTotals` and measurement flags. Output is a frozen `ProposalEvidence` carrying the observation window, every count, and the policy/engine versions in scope.

`ken_services/learning/reflect.py` then proposes. A model is **optional and bounded**: `ModelRole.REASONING` through the existing provider, given only the computed aggregate table (never page content), asked for a validated `ProposalDraft{title, rationale: ModelText, choice_index: int}` where `choice_index` selects from a **server-enumerated option set** that deterministic code generated (e.g. three candidate weight vectors derived from the disagreement data). The model therefore chooses and explains; it never emits a number that is stored, never names a field, and cannot reach a frozen field. Model disabled/unavailable/invalid → the deterministic rule picks option 0 and records `fallback_used` + `FallbackReason`, exactly as the agent nodes do. A static test (modelled on the ToolBox test) asserts that `evidence` is only ever assembled by `aggregate.py`.

**Deduplication.** `proposal_fingerprint = sha256(canonical_json(kind, target_kind, base_version, sorted changes))` for policies; `sha256(name, normalised instructions, input_schema, output_schema)` for skills. A partial UNIQUE index over open statuses makes a re-run idempotent. Near-duplicates use `change_signature = sha256(kind, target_kind, sorted change paths)`: a new proposal touching the same path set while an open one exists is inserted with `supersedes` set and the older row moved to `withdrawn` — newer evidence wins, nothing is silently merged. No embedding similarity; that would be over-design.

## 4. Decision 3 — skill registry and lifecycle

`skills`: `id uuid` PK, `name` (`^[a-z][a-z0-9_]{2,63}$`), `version int ≥ 1`, `description ≤500`, `instructions text ≤8000`, `input_schema jsonb`, `output_schema jsonb` (both JSON-Schema objects), `source` (`human|reflection`), `proposal_id` FK nullable, `evaluation_run_id` FK nullable, `evaluation_score double nullable`, `status` (`draft|offline_test|promoted|rejected|retired`), `promoted_by`, `promoted_at`, `created_at`. UNIQUE `(name, version)`; a partial unique index allows **at most one `promoted` row per name**. Content is immutable (`ken_skill_guard()`: only status/`promoted_*`/`evaluation_*` may change; `promoted` requires both set and an actor matching `^user:`). A revision is a new `version` row, never an edit.

**Skills get a shorter lifecycle than policies:** `proposed → offline_test → promoted`, plus `rejected` and `retired`. SHADOW and CANARY are deliberately omitted: **nothing consumes a skill at runtime in Phase 8**, so a shadow stage would be theatre. The only consumer in Phase 8 is the eval runner, which executes a skill's instructions through `ModelRole.REASONING` against the suite the skill declares — which is what makes TRD §17's `evaluation_score` a real number instead of a stored opinion. Wiring a promoted skill into the agent graph is Phase 9 and needs its own prompt-injection analysis (ASK item 5).

**The human promotion action.** `POST /learning/proposals/{id}/advance` with `{operation_id, to_status: "promoted", evaluation_run_id, note}`, permission `promote`, actor `principal.actor`. The service refuses unless: the eval run exists, `gate_result = pass`, its `proposal_fingerprint` equals the proposal's current fingerprint, its dataset manifests are unchanged, and it is newer than `promotion_max_eval_age_days`. Then it emits `skill.promoted` with `promoted_by = principal.username`, `evaluation_ref = str(evaluation_run_id)`, `previous_version` = the version it replaces. Agents cannot reach this route: the proposal's `proposed_by` must match `^(system|agent):` and the promotion transition's actor must match `^user:`, both as DB CHECKs.

## 5. Decision 4 — policy lifecycle, operationally

| Stage | What it actually does in this codebase | Gate to advance |
|---|---|---|
| `proposed` | A `learning_proposals` row plus a `policy_candidates` row holding the **full validated candidate document** as jsonb (so it is reproducible without a file). Zero production effect. | a human opens it |
| `offline_test` | `run_evaluation(mode=offline)`: the candidate document is loaded **in memory** and the Phase 3 pipeline is replayed over the eval corpus; scored against the committed baseline run. | `gate_result = pass` |
| `shadow` | After each real `make recommend`, `shadow.py` re-runs selection with the candidate **inside the same job**, writes **no** `link_plans`, and records one `policy_evaluations` row (`mode=shadow`) holding the plan-set diff: pairs added, dropped, re-ordered, and score deltas. Production output is unchanged — a test asserts zero plan rows written during a shadow pass. | `shadow_min_runs`, `shadow_min_plans` |
| `canary` | The candidate is used **for real** on a bounded slice: sources where `sha256(candidate_version + ':' + source_page_id)` falls in the first `canary_percent`. Its plans are stamped `ranking_policy_version = <candidate_version>` and go to the **normal human queue** — AUTO_APPROVE is off, so no canary link reaches production unapproved. | canary approval/edit rate vs the baseline cohort over the same window, min sample |
| `experiment` | A `policy_ab` experiment (§6). | the experiment ends and its comparison is computed |
| `promoted` | `make policy-promote` writes `policies/<kind>/<kind>.v<N+1>.yaml` from the stored candidate document. **This is the only file write in the phase, and only the CLI does it.** The API records the event. | human, with `evaluation_run_id` |
| `rejected` | Terminal, human, with a structured reason. | — |
| `rolled_back` | The promoted version is retired and its predecessor becomes active again. The old file is **never** edited or deleted. Recorded as a `promoted → rolled_back` transition plus a fresh `policy.promoted` for the **restored** version, whose `previous_version` is the rolled-back one and whose `evaluation_ref` is that version's original evaluation. Truthful, and needs no contract change. | — |

**What a proposal may change** (allowlist in `policies/learning/learning.v1.yaml`): `ranking.weights.*` (the contract validator still enforces non-negative, sum 1); `recommendation.selection.{min_relevance,min_score,max_plans_per_source,max_open_plans_per_target}`; `recommendation.retrieval.{semantic_k,lexical_k,graph_k,max_candidates_per_source,channels_enabled}`; `recommendation.{intent_compatibility,page_type_targets,geography_scores,underlinked_inbound_threshold}`; `agents.{critic.accept_min_confidence,max_plans_per_run}`. Everything else is a frozen field (§2).

**Architecture flag:** promotion changes the active policy *path*, and `Settings` is frozen, so promotion requires an operator restart. Hot reload would be an architecture change (ASK item 8).

## 6. Decision 5 — experiments (owed from Phase 7)

`experiments`: `id uuid` PK; `key` UNIQUE `^[a-z0-9][a-z0-9_-]{2,63}$`; `description ≤500`; `unit text` CHECK `= 'source_page'`; `kind` (`holdout|policy_ab`); `candidate_id` FK `policy_candidates` NULL (required for `policy_ab`, forbidden for `holdout`); `proposal_id` FK NULL; `treatment_fraction double` in `(0,1]`; `status` (`draft|active|ended`); `created_by` (actor pattern, `^user:`); `created_at`, `updated_at`, `started_at`, `ended_at`. No DELETE; status only `draft→active→ended`; content immutable after insert (`to_jsonb` guard, as in 0008).

`experiment_assignments`: `id bigint identity`; `experiment_id` FK; `source_page_id` FK `pages`; `arm` (`control|treatment`); `assigned_by`; `assigned_at`; UNIQUE `(experiment_id, source_page_id)`. Append-only (`ken_reject_mutation`). Assignment only while `draft`; the guard refuses an insert when the page already has an assignment in another experiment that is not `ended` (one active experiment per page).

**Assignment is deterministic and materialised:** `make experiment-assign` computes `arm` from the first 8 bytes of `sha256(key + ':' + str(source_page_id))` against `treatment_fraction` and writes the rows, so the arm stays auditable and stable even if the hash function ever changes.

**`SqlExperimentGuard.locked(*, source_page_id, target_page_id)` is true iff the source has a `control` assignment in an **active `holdout`** experiment.** A `policy_ab` locks nobody; the pipeline resolves the ranking policy per source from the arm. Backed by a per-run cached map, not per-pair SQL. One-line injection replaces `NoExperiments()` at `pipeline.py:891` and `review/service.py:316`.

**Attribution stays derived** (assignment × `[started_at, ended_at)` × the plan's live window from `link_plan_transitions`). No experiment column is ever added to `link_events` — a column-set test already pins that.

**Lift, given the Phase 7 caveats.** For `policy_ab` the **primary** metric is editorial (approval rate, edit rate per arm) because that stream is trustworthy; behavioural metrics are secondary and may only block. For a `holdout` the comparison is the target-page metric (GSC clicks, GA4 pageviews) between arms over the active interval, grouped **by deployment** rather than by link (ADR-0015 §12.7). **Permitted claim:** "observed difference between arms over [interval], N_control/N_treatment pages, with these confounder flags". **Forbidden:** statistical significance, causal lift from first-party counts alone, revenue, ranking causation. The UI carries the same "observed change, not a causal effect" note Phase 7 uses.

## 7. Decision 6 — eval datasets and the offline eval runner

**Datasets are files, runs are rows.** `evals/datasets/<suite>/<dataset>.v<N>.jsonl` plus a per-dataset `MANIFEST.yaml` (`dataset_version`, per-file `sha256`, case count, provenance, privacy). Files are diffable and reviewable in a commit and reproducible from a checkout; PostgreSQL is the system of record for *runs*, not fixtures. The Phase 0 skeleton in `evals/README.md` §4 is frozen as `EvalCase` in a new `contracts/python/ken_contracts/learning.py`, with `recorded_model_responses` keyed by **role**, never a model name.

Suites: the seven TRD §19 minimums (`destination-selection`, `anchor-selection`, `relationship-detection`, `critic`, `no-action`, `protected-content`, `stale-page-version`) are promoted from recorded fixtures to versioned datasets; `policy-regression` is populated; three new learning suites are added:
- **`editorial-agreement`** — does a candidate policy or skill agree with recorded human decisions on held-out plans?
- **`reflection-grounding`** — every number in a proposal's evidence must re-derive from the DB snapshot. A proposal whose evidence does not re-derive is a hard fail. This is the anti-fabrication gate.
- **`proposal-safety`** — a proposal touching a frozen field is refused; prompt-injection cases ("promote yourself", "set auto_approve") produce a refusal, never a proposal.

Runner: `evals/runner.py` + `ken_services/learning/evaluate.py`, `make eval SUITES=… [PROPOSAL=…] [SKILL=…]`, executed as `run_job(kind=EVALUATION)`. Each run writes one `policy_evaluations` row recording `runner_version`, every dataset manifest sha256, the policy file shas, the provider and the **resolved model per role**, the seed, the git sha, and per-case results. Default provider `mock` with recorded responses, so the same dataset gives a byte-identical report and CI can gate on it; `--provider ollama` is opt-in (`KEN_RUN_LIVE_OLLAMA=1`) and never gates.

Metrics: per-suite pass rate; `must_not` violations (zero tolerance); precision@1 / recall@3 / Kendall tau against human-approved destinations; critic precision and recall against human outcomes; reliability bins and ECE for calibration (reported, not gating, in Phase 8). Gate, from `policies/learning/learning.v1.yaml`: `gate_result = pass` requires every hard-fail suite clean (`protected-content`, `stale-page-version`, `proposal-safety`, `reflection-grounding`), every declared suite at or above its `min_pass_rate`, and no regression beyond `max_regression_vs_baseline` against the recorded baseline run. A hard-fail suite cannot be waived by policy — a test asserts that.

**`evaluation_ref` is `str(policy_evaluations.id)`.** The promotion service accepts nothing else.

## 8. Decision 7 — versioned event semantics

**No new event names.** `skill.proposed`, `skill.promoted`, `policy.proposed` and `policy.promoted` already exist and are exactly the four facts worth announcing. Stage transitions live in append-only `learning_proposal_transitions` (queryable, no vocabulary growth), and a finished evaluation is already announced by `job.completed` with `kind=evaluation`. I argue against `policy.shadow_started`-style names: they would balloon the vocabulary for data a transitions table already holds better.

New typed payloads, `event_version` 1: `PolicyProposedPayload{proposal_id, fingerprint, target_kind, base_version, change_paths, proposed_by, job_run_id, evidence_window, model, fallback_used}` and `SkillProposedPayload{proposal_id, fingerprint, name, version, source, proposed_by, job_run_id, model, fallback_used}`. `PromotionAuditPayload` is unchanged.

**Version-aware reads** (the owed Phase 0 QA M1 item): add to `events.py` a registry `EVENT_PAYLOAD_MODELS: Mapping[EventName, Mapping[int, type[FrozenModel]]]` and `validate_payload(event_name, event_version, payload) -> PayloadValidation` that **never raises** and returns `ok | unknown_version | invalid`. `list_events` and the learning readers re-validate every stored row through it, and `EventRecord` gains an additive `payload_status: Literal["ok","unknown_version","invalid"] = "ok"`. Rule: an old row is read by the model registered for **its** `event_version`; a version with no registered model is returned verbatim as `unknown_version`. A stored row is **never rewritten**. This retro-covers `analytics.updated` and the `link.*` payloads, and it removes the standing risk that one bad row makes the audit log unreadable forever.

## 9. Migration `0009_learning` (0007/0008 style: `op.f()` names, `ck_*` CHECKs, frozen enum copies)

1. **`learning_proposals`** — `id uuid` PK; `kind` (`policy|skill`); `status` (`proposed|offline_test|shadow|canary|experiment|promoted|rejected|rolled_back|withdrawn`); `target_kind` (`ranking|recommendation|agents` for policies, NULL for skills); `base_version text`; `title ≤200`; `rationale ≤2000`; `body jsonb`; `evidence jsonb`; `fingerprint` hex64; `change_signature` hex64 NULL; `proposed_by` (ACTOR_PATTERN, CHECK `^(system|agent):`); `job_run_id` FK `job_runs`; `model text` NULL; `fallback_used bool`; `supersedes` self-FK NULL; `created_at`, `updated_at`. Partial UNIQUE index on `fingerprint` where status is open. `ken_learning_proposal_guard()` — a DEFERRABLE INITIALLY DEFERRED constraint trigger modelled on `ken_link_plan_status_guard`: content columns immutable, only `status`/`updated_at` may change, the pair must be in `PROPOSAL_TRANSITIONS`, and a matching `learning_proposal_transitions` row must exist **in the same transaction**. No DELETE.
2. **`learning_proposal_transitions`** — `id bigint identity`; `proposal_id` FK; `from_status`, `to_status`; `actor` (ACTOR_PATTERN); `note ≤500`; `evaluation_run_id` FK NULL; `created_at`. Append-only. CHECK: `to_status IN ('promoted','rejected','rolled_back')` requires `actor ~ '^user:'`; `to_status = 'promoted'` requires `evaluation_run_id IS NOT NULL`. **This is where "no self-promotion" is enforced in the database.**
3. **`policy_candidates`** — `id uuid` PK; `proposal_id` FK; `policy_kind`; `base_version`; `candidate_version text` UNIQUE; `document jsonb` (CHECK object); `document_sha256` hex64; `created_at`. Append-only.
4. **`skills`** — as §4. `ken_skill_guard()`; partial unique index `(name) WHERE status='promoted'`.
5. **`policy_evaluations`** (TRD §5's name, widened) — `id uuid` PK; `job_run_id` FK; `mode` (`offline|shadow|canary`); `proposal_id`, `candidate_id`, `skill_id` FKs NULL (CHECK: exactly one of proposal/skill set); `proposal_fingerprint` hex64 NULL; `suites jsonb` array; `dataset_manifests jsonb` (name → sha256); `baseline_evaluation_id` self-FK NULL; `metrics jsonb`; `gate_result` (`pass|fail|inconclusive`); `gate_reasons jsonb`; `runner_version`, `model_provider`, `model_role_models jsonb`, `seed bigint`, `git_sha`; `started_at`, `finished_at`. Append-only.
6. **`experiments`**, 7. **`experiment_assignments`** — as §6.

Downgrade drops the triggers, the two new guard functions and the seven tables, returning exactly to the 0008 state.

## 10. API and UI

Routes under `/learning`, all GET on `Permission.READ` except one:

- `GET /learning/proposals`, `GET /learning/proposals/{id}` (read)
- `GET /learning/evaluations`, `GET /learning/evaluations/{id}` (read)
- `GET /learning/skills`, `GET /learning/skills/{name}` (read)
- `GET /learning/experiments`, `GET /learning/experiments/{key}` (read)
- `POST /learning/proposals/{id}/advance` — **the only write route**, `Permission.PROMOTE`

**New permission, justified.** `promote` is added to `Permission` and granted **only to `operator_admin`**. Reusing `review` would let every reviewer promote a policy, which is categorically different from editorial judgement and is ASK-level in AUTONOMY_AND_PERMISSIONS.md; reusing `read_settings` would disguise a write as a read. One enum member, one role, and the RBAC-matrix test extends to cover it. `/settings/policies` additionally gains `PolicyKind` `"learning"` (the same three-place additive edit ADR-0015 §12.9.10 recorded).

**Everything else is CLI-only** — `make reflect`, `make eval`, `make shadow`, `make experiment-create|assign|start|end`, `make policy-promote` — matching ADR-0013 §7 and ADR-0015 §6: no UI button starts a model run, writes a policy file or triggers egress.

**UI** (`apps/web/app/learning/**`, server-rendered, hand-written SVG, no chart dependency): `/learning` is a lifecycle board with one column per stage; `/learning/proposals/[id]`; `/learning/evaluations/[id]`; `/learning/skills`; `/learning/experiments`.

**What a human must see before promoting**, on one screen: the exact parameter diff (path, from, to); an explicit "frozen fields untouched" assertion; the evidence table with every number and the window it came from; the passing eval run with per-suite pass rates, the baseline it beat, the dataset versions and shas, and the provider/model/seed; every hard-fail suite shown clean; the shadow diff; the canary approval and edit rates; the experiment comparison with its confounder chips and the "observed change, not causal" note; who proposed it and whether a model answered or it fell back; **the exact file path the promotion will write**; and the rollback path (which version would be restored). The button is disabled unless an eval run bound to the current fingerprint passed.

## 11. Tests and evals

Migration: round trip; content immutability; a status change without a transition row fails; no DELETE anywhere; at most one promoted skill per name; assignment append-only; one active experiment per page; `to_status='promoted'` refused without `evaluation_run_id` or with an `agent:` actor. Contracts: frozen-field refusal; fingerprint canonicalisation; `validate_payload` over every event name × version including an unknown version and a corrupt payload; a promotion event still refused without a human and an eval ref. Services: promotion refused with a stale, foreign, failed or fingerprint-mismatched eval ref, and refused for a `reviewer` role; dedupe collapses a re-run; a shadow pass writes zero plan rows (row-count assertion around it); canary slice membership is deterministic. Experiments: deterministic assignment; the holdout guard blocks the pair at both injection points; derived attribution equals assignment × interval; `link_events` still has exactly its eight columns. Runner: the same dataset twice gives an identical report; a `must_not` hit fails the gate; a hard-fail suite cannot be waived. Evals: `evals/learning/lifecycle_scenario.json`, a golden fixture pinning the exact stage sequence and every refusal. New hard gates (zero tolerance): nothing reaches `promoted` without a `user:` transition and an eval ref; no policy file is written by anything but the CLI; no model-produced number appears in `evidence`.

**No new dependency.** The only statistics are counts, means, ratios and reliability bins — `math` and `statistics` suffice. `pyyaml` and JSONL are already approved. If anyone proposes significance testing, that is where numpy/scipy would appear, and this ADR's answer is that we make no significance claims.

## 12. Workstreams (frozen interfaces first; single-owner files)

**W0, orchestrator, before fan-out:** `contracts/python/ken_contracts/learning.py`; the additive edits to `events.py` (payload registry, `validate_payload`, the two new payloads), `jobs.py` (`REFLECTION`), `auth.py` (`PROMOTE`), `controlplane.py` (`PolicyKind`, `EventRecord.payload_status`); `migrations/versions/0009_learning.py` and `ken_core/db/models/learning.py`; `ken_core/config.py`; `policies/learning/learning.v1.yaml`; `apps/api/ken_api/routers/learning.py` signatures and `deps.py`; `Makefile`; `contracts/openapi/openapi.json`; `apps/web/lib/types.gen.ts`; `docs/CONTRACTS.md` §20 and this ADR. Frozen signatures: `reflect.run_reflection(...)`, `evaluate.run_evaluation(...)`, `promote.PromotionService.advance(...)`, `experiments.SqlExperimentGuard`.

**W1, reflection and proposals:** `ken_services/learning/{aggregate,reflect,proposals,dedupe}.py`, the REASONING slot and its prompt, tests.
**W2, eval runner and datasets:** `evals/runner.py`, `evals/datasets/**`, `ken_services/learning/evaluate.py`, scorers, the three new suites, populating the seven TRD suites.
**W3, lifecycle and experiments:** `ken_services/learning/{lifecycle,shadow,canary,promote,experiments}.py`, `policy_candidates`, the one-line `SqlExperimentGuard` injections at `pipeline.py:891` and `review/service.py:316`, `evals/learning/`.
**W4, API:** router bodies, the RBAC matrix extension, API integration tests.
**W5, frontend:** `apps/web/app/learning/**`, `components/learning/**`, `lib/learning.ts`, the nav entry.

## 13. PRD/TRD amendments I recommend rather than implementing as written

1. **TRD §17's single `evaluation_score`** is too thin. Amend to a reference to a `policy_evaluations` run plus a per-suite metric map; keep a scalar for display only.
2. **PRD §10's six stages** are right for a ranking policy and meaningless for `agents.critic.accept_min_confidence` or a skill. Amend to: six stages are the *maximum* path, and the gate policy declares the minimum required path per kind — ranking/recommendation: full path; agents: `offline_test → shadow → promoted`; skills: `offline_test → promoted`.
3. **PRD §8** as written implies learning from CTR and GSC. Amend to: behavioural is advisory and search is diagnostic; only editorial feedback and experiment arms may gate a promotion.
4. **TRD §19** should gain `policy-regression`, `editorial-agreement`, `reflection-grounding` and `proposal-safety`.
5. **TRD §5** keeps the name `policy_evaluations`, widened to skills and to the `offline|shadow|canary` modes.

## 14. Risks and ASK items for the user

1. **ASK — Phase 8 promotes nothing.** It builds the mechanism; `PROMOTE_SKILL` / `PROMOTE_POLICY` stay ASK. Confirm the phase exits with zero promotions, or name one policy for a supervised dry run.
2. **ASK/decision — the new `promote` permission**, `operator_admin` only. It changes the RBAC matrix.
3. **Decision — does CANARY place real links under a candidate policy?** My recommendation: yes, bounded, with every plan still human-approved. The alternative is to stop candidates at SHADOW until there is real traffic.
4. **Decision — holdout experiments deliberately withhold links from control pages**, a real SEO cost. Confirm the fraction cap (proposed 10 %, hard max 20 %) and that it is acceptable at all; otherwise only `policy_ab` is built.
5. **Decision — a promoted skill changes no agent behaviour in Phase 8.** Confirm, or authorise the Phase 9 consumer design (and its prompt-injection analysis) now.
6. **Data risk, flagged hard.** The synthetic corpus is 65 pages and 48 plans with a handful of review actions. **Every Phase 8 number will be a demonstration, not evidence**, and nothing may be promoted on it. Real editorial volume is the gate on this phase being more than plumbing.
7. **ASK — the deferred GSC query import** (`page_query_metrics_daily`, `--queries`) is needed before search learning has any data, and it is Google egress.
8. **Architecture flag — promotion needs an operator restart**, because `Settings` is frozen and the active policy path comes from it. Hot policy reload would be a material architecture change.
9. **Accepted limitations:** outcomes stay correlational; first-party counts stay spoofable; `overlapping_changes` forces grouping by deployment; ECE is reported but not gating until Phase 9; no significance testing, by design.

---

### Critical files for implementation
- /Users/abhinav/Documents/Projects/Ken-Interlinking OS/contracts/python/ken_contracts/events.py
- /Users/abhinav/Documents/Projects/Ken-Interlinking OS/contracts/python/ken_contracts/recommendations.py
- /Users/abhinav/Documents/Projects/Ken-Interlinking OS/migrations/versions/0008_analytics.py
- /Users/abhinav/Documents/Projects/Ken-Interlinking OS/services/ken_services/recommendations/pipeline.py
- /Users/abhinav/Documents/Projects/Ken-Interlinking OS/services/ken_services/review/controlplane.py

(Also load-bearing: `contracts/python/ken_contracts/{auth,jobs,review,analytics}.py`, `services/ken_services/recommendations/filters.py` lines 108–117 for `NoExperiments`, `services/ken_services/review/service.py:308-316`, `services/ken_services/jobs/runner.py` for `run_job`, and `evals/README.md` §4 for the fixture skeleton being frozen.)

## 15. Amendments from the pre-fan-out architecture review (binding)

Verdict: **APPROVE WITH REQUIRED CHANGES.** The review verified the draft's code claims against the source and found three wrong (§15.1). It also found that the phase's central safety claim was **decorative** — see §15.3, the most important part of this ADR. These amendments override §1–§14 wherever they conflict.

### 15.1 Correctness against the code

**Verified correct** (don't re-check): `JobKind.EVALUATION` (`contracts/python/ken_contracts/jobs.py:46`); all four event names (`events.py:79-82`); `LEARNING_CAPTURED` (`recommendations.py:129`), `MEASURED → LEARNING_CAPTURED` in `ALLOWED_TRANSITIONS` (`:155`), `TRANSITION_AUTHORITY` system-only (`:206`); `ExperimentGuard.locked(self, *, source_page_id: UUID, target_page_id: UUID) -> bool` (`:499-506`); `NoExperiments()` injected at **`pipeline.py:891`** and **`review/service.py:316`** — both line numbers exact; `review_actions` append-only (`0006_review_control_plane.py:324`), `agent_run_plans.verdict` (`0005_agent_runs.py:251`), `link_measurements` append-only (`0008_analytics.py:49`); `REASONS_BY_ACTION` (`review.py:116`, enforced `:164,:218`); `/settings/policies` per-file sha256 (`review/controlplane.py:232-260`); `PolicyKind` a closed Literal (`controlplane.py:56`) needing exactly three edits, as ADR-0015 §12.9.10 records; `Settings` `frozen=True` (`ken_core/config.py:166`); the AST static-test pattern exists (`tests/unit/agents/test_boundaries.py:84-128`); ADR-0015 §12.8 does defer experiments to Phase 8; TRD §19 names exactly those seven suites.

**Wrong or incomplete:**

1. **"`EventEnvelope` already refuses a `*.promoted` event without a human `promoted_by`" — wrong on "human".** `events.py:113-122`: `promoted_by: str = Field(min_length=1)` and `evaluation_ref: str = Field(min_length=1)`. `promoted_by="agent:reflector@1"` validates. The docstring asserts human; nothing enforces it. No pattern, no FK, no UUID type.
2. **`ExperimentGuard` is `@runtime_checkable Protocol`** (`recommendations.py:498`), not "a frozen protocol".
3. **The eval suites do not exist as fixtures.** All eight `evals/suites/*/` dirs contain only a 24–25 line `README.md`. Nothing is "promoted from recorded fixtures"; every case must be authored. This is the draft's largest effort mis-estimate.
4. **`evals/README.md:97`** is headed "Fixture format (**Proposed — NOT frozen, NOT implemented**)", says "One case per file, or JSON Lines per suite. The choice is deferred to Phase 8", and the skeleton is **YAML**. Choosing JSONL is legitimate but it is a decision, not a freeze. The skeleton's `decision_class` field (`:101`, "for TRD §20 per-class metrics") is **omitted from the draft's `EvalCase`** — that is the field Phase 9 needs.
5. **`make policy-promote` writing `policies/<kind>/<kind>.v<N+1>.yaml` is wrong for ranking.** The file is `policies/ranking/ranking_weights.v1.yaml`, and the *active* ranking policy is resolved from `policies/recommendation/recommendation.v1.yaml:18 ranking_policy:` by `resolve_ranking_policy` (`recommendations/policy.py:95-108`) — **not** from `Settings`. Promoting ranking weights therefore requires a second, atomic edit to the recommendation document (itself a policy promotion). Unaddressed. The "restart because `Settings` is frozen" claim is right for taxonomy/recommendation/agents/analytics (`config.py:287-334`) only.
6. **"no new job kind needed except one"**: `JobKind` already labels `EVALUATION` Phase 8 and `AUTONOMY_SHADOW` Phase 9 (`jobs.py:46-49`), and its docstring says "one per phase". `REFLECTION` makes two for Phase 8 — amend the docstring rather than quietly break it.
7. **`validate_payload` is an incomplete fix.** `list_events` (`ken_core/db/repositories/events.py:85-97`) also calls `EventName(row.event_name)`, so an *unknown event name* breaks listing too — not just a bad payload.

### 15.2 Safety invariants (critical)

Self-promotion is made hard, not impossible. Three of four load-bearing claims are convention.

**(a) The `learning_proposal_transitions` CHECK is bypassable in two transactions.** 0007 works because `link_plan_transitions` has a `txid` column and the guard requires `t.txid = pg_current_xact_id()` (`0007_controlled_deployment.py:435`). The draft's §9.2 table has **no `txid`**, so its §9.1 "in the same transaction" can only be `EXISTS(...)`. The transitions table is append-only, so any DB writer can INSERT a `proposed→promoted` row with `actor='user:admin'` and an `evaluation_run_id` in txn 1 (the CHECK tests string shape only), then UPDATE the proposal in txn 2. Real hole in the phase's central invariant.

**(b) The DB can only check an actor's *shape*, never its authenticity.** `actor ~ '^user:'` is a string predicate; only `principal.actor` from the session binds it to a human (`auth.py:17-18` states that rule). So the mechanism is "API route + RBAC", and the CLI, a data-fix, a test helper or a mis-set reflection actor can forge it. The 0008 pattern is the answer and already exists: a transaction-local GUC (`ken.analytics_writer`, `0008:50`), verified at the Phase 7 freeze to not leak across transactions (ADR-0015 §12.9).

**(c) The frozen-field allowlist is convention, not a contract boundary.** §2 says "refused at the contract boundary, not merely at review", but §5 puts the allowlist in `policies/learning/learning.v1.yaml` — a human-editable document. The draft's own argument ("the whole `analytics` policy is frozen because measurement must never be tuned by the thing it measures") applies verbatim to the frozen list itself.

**(d) The bounded model role is the strongest part and genuinely a mechanism.** A server-enumerated `choice_index` means the model cannot emit a stored number or name a field — provided the index is validated against the actual option count in the same call, and the option set is persisted in `evidence` so the choice is reconstructable. The proposed AST test is the right enforcement and the pattern exists.

**(e) Prompt injection: the draft contradicts itself.** §2 lists `note`, `anchor_before/after`, `target_before/after` as editorial sources; §3 claims the model gets "only the computed aggregate table (never page content)". Anchor text **is** page content (`recommendations.py:531`: "'edited' must still be copied verbatim from the block text") and notes are free text ≤500. Reflection is corpus-level, so one poisoned anchor steers *every* proposal — a strictly worse blast radius than the per-page agent prompt. Note the existing mitigation the draft doesn't reuse: `agents/ken_agents/prompts.py:29-35,65-73` (opaque `c0…` refs, single JSON `DATA` value, explicit untrusted-data system message).

**(f) No SECURITY DEFINER anywhere** (grep clean), so no gap there — but every guard is a trigger, so a superuser with `session_replication_role = replica` disables all of them. The ADR should say plainly that the DB guards defend against application bugs, not a DB superuser. (Credit where due: `PoliciesView.auto_approve_enabled: Literal[False]`, `controlplane.py:69`, is a real contract-level pin.)

### 15.3 Scope: keep / cut / defer

- **Six-stage path → CUT to per-kind minimum.** Its §13.2 is right; make it the design, not a doc amendment. Phase 8: `proposed → offline_test → promoted` for all kinds, `shadow` optional.
- **SHADOW → KEEP**, ranking/recommendation only. Zero production effect, in-process, and the only stage that yields a real diff on 65 pages.
- **CANARY → DEFER to Phase 9.** Marginal information from 48 plans, and it is what forces ASK 3.
- **Holdout experiments → CUT.** 10% of 65 pages is 6 pages. That is an SEO cost with no statistical return, and the design forbids significance claims anyway.
- **`policy_ab` → DEFER the practice, KEEP the plumbing.** Build `experiments`, `experiment_assignments` and `SqlExperimentGuard` (owed by ADR-0015 §12.8; the hook is already wired at both injection points, so it is cheap and closes a real loose end), but ship with `draft` status only and no activation path.
- **Three new suites → KEEP `reflection-grounding` + `proposal-safety`** (anti-fabrication and anti-injection gates, fully synthetic, corpus-size-independent). **DEFER `editorial-agreement`** — it needs held-out human decisions that don't exist.
- **The seven TRD §19 suites → KEEP, top priority.** A genuine TRD requirement, currently non-existent (finding 3). This is where the phase's effort belongs.
- **Payload registry + `validate_payload` → KEEP.** Discharges the owed Phase 0 item, fixes a confirmed "one bad row breaks listing forever" risk, purely additive. Extend to unknown event *names*.
- **`policy_candidates` full documents → KEEP.** What makes `offline_test` replayable without a file.
- **`change_signature` superseding → CUT.** Auto-withdrawing a human-visible proposal is a silent state change; the `fingerprint` partial-unique index already gives idempotence. Keep `supersedes` as a column set only by a human.
- **Lifecycle board → TRIM** to list + detail. The §10 pre-promotion disclosure list is excellent and must stay in full; a kanban column per stage is UI for stages we're cutting.
- **`promote` permission → KEEP.** The reasoning is correct (`ROLE_PERMISSIONS`, `auth.py:63-75`), and `apps/web/lib/audit.ts:41` + `apps/web/tests/review.test.ts:66-70` enumerate these exhaustively, so a miss fails the build.

### 15.4 The data-volume problem

65 **synthetic** pages (`PHASE_2_REPORT.md:44`), 48 plans (`PHASE_6_REPORT.md:49`), and the review actions on them are scripted — so the one stream the design calls "trustworthy" is the one with no real data. Documentation is not enough. **Bake it in:** (i) `Settings.learning_demonstration_mode: bool = True`, with `PromotionService.advance` refusing `to_status='promoted'` with a typed `DEMONSTRATION_MODE` error while set; (ii) a `min_sample_count` floor per decision class (≈30) below which `gate_result` is `inconclusive`, never `pass`; (iii) a persistent banner on every `/learning` page naming the corpus size and "no proposal on this data may be promoted". The phase then exits with zero promotions by construction, which answers its ASK 1 without asking.

### 15.5 Experiments

Deterministic **and** materialised is right — recomputable and recorded, surviving a hash change; `UNIQUE (experiment_id, source_page_id)` + append-only is the correct shape. `locked()` returning true for a `control` assignment in an active `holdout` is semantically correct: the guard's contract is "an active experiment forbids modification" (`recommendations.py:500`), and a control page must receive no new links. A per-run cached map is right (per-pair SQL would be a round trip per candidate). No-op for `policy_ab` is also correct — both arms get links, only the ranking policy differs.

**One real design bug:** arms are assigned by **source** page, but §6 says the holdout comparison is "the target-page metric between arms". Those are different pages, and nothing in `locked()` stops a control page from receiving *inbound* links — so the arm is contaminated. Either assign holdout arms by target page, or state that a holdout measures source-side effects only.

And yes: withholding links is a real editorial cost on the client's live site, so it cannot be a default. **The proposed default (10%, hard max 20%) is not safe as a default.** Safe default: cap the `kind` enum to `policy_ab` for Phase 8 and build no holdout at all until the user says otherwise.

### 15.6 Phase 9 readiness

Mostly yes — `JobKind.AUTONOMY_SHADOW` already exists. Two gaps that would force rework:

- **Decision class is not a contract.** TRD §20 requires per-class `sample_count, approval_rate, edit_rate, rollback_rate, positive_outcome_rate, calibrated confidence, autonomy_level`; the draft's `aggregate.py` covers the first four and omits `positive_outcome_rate` and calibrated confidence per class. Make `DecisionClass` a typed contract with a canonical string form in `ken_contracts/learning.py`, put it on `EvalCase` (as `evals/README.md:101` already anticipates), and key `aggregate.py` on it.
- **Calibration is a blob.** Require `policy_evaluations.metrics` to carry a `by_decision_class` object keyed by that canonical form, so Phase 9 reads it without a schema change.

The "behavioural may only block, never pass" rule is exactly right for Phase 9 and must be **code** — a `BlockOnly` wrapper in the gate evaluator — not a row in a §2 table. Otherwise `AUTO_APPROVE_ELIGIBLE` could be reached on spoofable first-party counts. (Naming: TRD §20 says `AUTO_APPROVE`, the brief says `AUTO_APPROVE_ELIGIBLE` — pick one in the ADR.)

### 15.7 Required changes before freezing contracts

1. Add `txid bigint NOT NULL DEFAULT pg_current_xact_id()` to `learning_proposal_transitions` and make `ken_learning_proposal_guard()` match it, exactly as `0007:430-441` does. Non-negotiable; without it §9.2 is decorative.
2. Add a transaction-local GUC `ken.learning_promoter` on the 0008 `ken.analytics_writer` model; the guard refuses a `promoted` transition unless it is set, so exactly one code path can promote.
3. `PromotionService.advance` takes an `AuthenticatedPrincipal`, never an actor string. Add an AST test (test_boundaries.py pattern) asserting no module under `ken_services/learning/` contains a `"user:"` literal and only `promote.py` writes `learning_proposal_transitions`.
4. Tighten `PromotionAuditPayload` (`events.py:113-122`): `promoted_by` constrained to `^user:` / `USERNAME_PATTERN` (`auth.py:31`), `evaluation_ref` constrained to a UUID string. Makes the draft's §1 claim true.
5. Move the frozen-field set and the change allowlist into `ken_contracts/learning.py` as `Final frozenset`s of dotted paths, enforced by a validator on the proposal body model. The YAML may only **narrow**; a test asserts it cannot widen.
6. Reflection prompt rule: enum codes, integers and floats only — no `note`, no anchor strings, no URLs, no titles. Test the rendered prompt against a character/field whitelist. If free text is ever needed, reuse the `DATA` + opaque-ref envelope from `agents/ken_agents/prompts.py`.
7. `ProposalDraft.choice_index` validated against the live option count; persist the full option set in `evidence`.
8. Promotion must be a **two-file atomic** operation for `ranking`: the new `policies/ranking/ranking_weights.v<N+1>.yaml` plus the `ranking_policy:` pointer in the recommendation document. Correct the filename convention in §5.
9. `make policy-promote` must refuse to write unless a `promoted` transition with a `^user:` actor and an `evaluation_run_id` already exists, and must never create one.
10. Add `Settings.learning_demonstration_mode` (default `True`) blocking promotion, the `min_sample_count` → `inconclusive` floor, and the UI banner (§5 above).
11. Add `DecisionClass` as a typed contract; add `decision_class` to `EvalCase`; require `metrics.by_decision_class`; add `positive_outcome_rate` to `aggregate.py`.
12. Behavioural signals pass through a `BlockOnly` wrapper in the gate evaluator.
13. Scope cuts per §4: drop `holdout` from the `kind` enum, cut `change_signature`, cut CANARY, defer `editorial-agreement`, trim the UI.
14. Extend `validate_payload` to unknown event *names*, not only payload versions; amend the `JobKind` "one per phase" docstring.
15. Correct §1's two misstatements (human `promoted_by`, "frozen protocol") and §7's "promoted from recorded fixtures" — the suites are empty.

**ASK items that are genuinely the user's:** #4 (withholding links from live pages — though #13 makes it moot for Phase 8); #7 (GSC query import — Google egress, a CLAUDE.md ASK); #8 (hot policy reload — a material architecture change, also moot while promotion is blocked).

**Decide yourself, safely:** #1 — don't ask, *enforce* zero promotions in code and report it; #2 — the `promote` permission strictly narrows who can promote and CLAUDE.md's ASK covers changing auth *settings*, not adding a least-privilege permission; decide it and record it in the ADR; #3 — defer canary, no question needed; #5 — take the conservative option (a promoted skill changes no agent behaviour) and defer the Phase 9 consumer design with its injection analysis; #6 — a design constraint to bake in, not a question; #9 — just record the limitations.

**Doc conflict to note:** `evals/README.md` §5 sources its per-class metrics from **TRD §20**, while the draft cites §19 throughout for learning metrics; §19 is only the suite list. Worth one line so Phase 9 looks in the right place. An ADR **is** required — this is new architecture (seven tables, a new job kind, a new permission, a new service package); ADR-0016 is the right vehicle, with the amendments above folded in as binding before fan-out.

## 16. Orchestrator decisions on the open items

The review separated genuine user decisions from ones to settle safely here. Settled here:

1. **Phase 8 promotes nothing, enforced in code — not asked.** `Settings.learning_demonstration_mode` defaults `True` and `PromotionService.advance` refuses `promoted` with a typed `DEMONSTRATION_MODE` error while it is set. A `min_sample_count` floor (30 per decision class) makes a gate `inconclusive` rather than `pass` below it, and every `/learning` screen carries a banner naming the corpus size. The phase therefore exits with zero promotions **by construction**, which is the standing rule (promoting a policy or skill into real behaviour is the user's decision) expressed as a mechanism instead of a promise.
2. **The `promote` permission is added**, granted to `operator_admin` only. It strictly *narrows* who may promote; the alternative — reusing `review` — would let every reviewer promote a policy, which is a categorically different act from editorial judgement. CLAUDE.md's ASK covers changing authentication *settings*, not adding a least-privilege permission.
3. **CANARY is deferred to Phase 9.** 48 plans yield almost no information, and deferring it removes the only reason to ask whether a candidate policy may place real links.
4. **Holdout experiments are cut entirely** — not merely defaulted off. `experiments.kind` is capped to `policy_ab`, so no code path can withhold links from a page. Withholding links from live pages is a real editorial and SEO cost and remains the user's decision; `holdout` is re-introduced only on their instruction. This also fixes the review's arm-contamination bug by removing its cause.
5. **A promoted skill changes no agent behaviour in Phase 8.** The eval runner is the only consumer. Wiring a skill into the agent graph needs its own prompt-injection analysis and belongs to Phase 9.
6. **`policy_ab` ships as plumbing only**: the tables, the deterministic assignment and the real `SqlExperimentGuard` (owed by ADR-0015 §12.8, with the hook already wired at both injection points), with `draft` status and no activation path.
7. **Behavioural signals pass through a `BlockOnly` wrapper in the gate evaluator** — code, not a table in a document, so a spoofable first-party count can never pass a gate.
8. **`AUTO_APPROVE` is the name used**, matching TRD §20; `AUTO_APPROVE_ELIGIBLE` from the roadmap wording is the same decision class.

Remaining **user** decisions, recorded in NEXT_STEPS.md and neither built nor enabled here:

- **Holdout experiments** on real content (an SEO cost; cut above).
- **The deferred Search Console query import** (`page_query_metrics_daily`), without which search learning has no data at all. It is Google egress, so it is ASK.
- **Hot policy reload.** Promotion changes the active policy path and `Settings` is frozen, so a promotion needs an operator restart. Hot reload would be a material architecture change; it is also moot while promotion is blocked.

**Stated plainly, because it limits everything above:** the corpus is ~65 synthetic pages, 48 plans and scripted review actions. The one stream this design calls trustworthy — human editorial decisions — is the stream with no real data. Phase 8 therefore delivers the learning *machinery* and an honest demonstration of it. It is not evidence about Ken Research's site, and no number it produces should be read as such.
