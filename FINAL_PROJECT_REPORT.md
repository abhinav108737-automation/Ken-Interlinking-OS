# Final Project Report — Ken Research Agentic Interlinking OS

**Status: the local product is complete and ready for production integration.** Everything described
here runs on one machine against a **simulated CMS and a synthetic corpus**. Nothing has been pushed,
deployed, or pointed at a real CMS, a real Ken Research page, a real analytics property or any Google
service. The default operating mode, `shadow`, publishes nothing.

"Ready for production integration" is a precise claim. It means the engineering is finished and
reviewed and the remaining work is external: credentials, a real CMS contract, real traffic and real
review history (section 20). It does **not** mean the system has been shown to work on Ken Research's
real content. Every statement about quality on real content is unproven (section 21).

---

## 1. Executive summary

The system observes a content site, understands it, proposes contextual internal links, decides under
a versioned policy whether each may be published **without a human**, publishes through a controlled
CMS adapter, verifies the result, rolls back automatically on failure, measures outcomes, and learns,
while policy and skill promotion stays a governed human act that the database itself refuses to let
anything else perform.

```
OBSERVE → UNDERSTAND → DISCOVER → RETRIEVE → RANK → PLACE → CRITIQUE → POLICY GATE
   → AUTONOMY DECISION → VALIDATE → PUBLISH → VERIFY → MEASURE → LEARN → OPTIMIZE → REPEAT
```

Three decisions were taken by you for this final pass and are implemented as ADRs:

- **Qdrant** is the semantic retrieval layer; PostgreSQL stays the source of truth and Qdrant can be
  rebuilt entirely from it (ADR-0017). pgvector was **removed**, not bypassed.
- **Publishing is autonomous and policy-governed**; there is no approval inbox in the normal flow
  (ADR-0018). Humans monitor, administer policy, investigate blocks and rollbacks, and handle
  exceptions.
- **Governed learning**: shadow is implemented, canary and experiment are defined but deferred,
  holdouts are built but disabled, and nothing can be promoted while demonstration mode is on
  (ADR-0016).

**What a first installation does.** `shadow` records what it *would* have done and changes nothing.
`guarded` publishes nothing until a decision class has been calibrated from real review history.
`autonomous` **does** publish, including uncalibrated plans, under the strict verification profile.
"Plain `AUTO_PUBLISH` is unreachable without history" means exactly that and no more — it must not be
read as "nothing publishes". Reaching `autonomous` takes three deliberate settings (section 8).

## 2. Final architecture

```
                    ┌──────────────────────────── apps/web (Next.js, canonical UI) ────────────────────────────┐
                    │  server-rendered; session token only in lib/api.ts; no DB access; no Electron imports     │
                    └───────────────────────────────────────────┬──────────────────────────────────────────────┘
                                                                │ HTTPS/JSON (typed, generated from OpenAPI)
                    ┌───────────────────────────────────────────▼──────────────────────────────────────────────┐
                    │  FastAPI  — the only application boundary; RBAC deny-by-default (viewer/reviewer/admin)    │
                    └───────────────────────────────────────────┬──────────────────────────────────────────────┘
        api  →  services  →  core  →  contracts                │
  ┌──────────────────────┬──────────────────────┬───────────────▼──────┬──────────────────┬────────────────────┐
  │ inventory, content   │ recommendations:     │ autonomy: blocks,    │ deployment: splice│ analytics:         │
  │ intelligence, graph  │ 6 generators, filters│ decision, publish,   │ mutation, verify, │ collector, GA4/GSC │
  │ metrics              │ ranking, anchors,    │ rollback, budget     │ rollback          │ CSV, attribution   │
  │                      │ placement            │                      │                   │                    │
  ├──────────────────────┴──────────┬───────────┴──────────────────────┴───────────────────┴────────────────────┤
  │ agents (LangGraph): critic reviews plans and hands them on; never publishes   learning: reflect, eval, shadow │
  └─────────────────────────────────┬──────────────────────────────────────────────────────────────────────────┘
                                    │
   PostgreSQL 16 (source of truth, 127.0.0.1:5433)    Qdrant v1.19.1 (derived index, 127.0.0.1:6333)    Ollama (local models)
```

Rules that hold throughout: dependency direction `api → services → core → contracts`; every LLM output
that crosses a boundary is validated with Pydantic; no LLM writes to storage; deterministic safety
lives outside any model; every mutation is idempotent, version-aware, auditable and reversible.

## 3. Implemented capability list

| Area | What exists |
|---|---|
| Inventory | Crawl (operator-configured host only), JSONL export and mock-CMS ingestion; normalisation; immutable page versions; link extraction and resolution; protected-page rules. |
| Content intelligence | Deterministic analysis (taxonomy, entities, page type, geography); embeddings through the model provider; relationships. |
| Graph | In/out degree, internal PageRank, hub and authority, crawl depth, orphan / nearly-orphan / bridge, components, clusters — in memory, persisted to `page_graph_metrics`. No graph database; an experiment seam only for any learned model. |
| Candidates | Six independent generators (semantic, lexical, topic, graph, analytics, orphan recovery); every candidate records which nominated it; signals stay separate. |
| Ranking | Versioned weights in policy; full per-signal breakdown, confidence, policy version and source attribution. |
| Anchors and placement | Real source text; context-hash and block identity (not fragile DOM indexes); before/after preview; fails safely when the source version is stale. |
| Critic | `ACCEPT / REJECT / NO_ACTION` as a *signal*. A failed, unparseable or `uncertain` answer is `NO_ACTION` and never a publish (ADR-0019). |
| Autonomy | 15 deterministic block codes, three decisions, three modes, calibration, risk tiers, retry budget and cool-down, strict verification profile. |
| Publishing | Two-transaction durable intent, per-source lock, expected-version check, splice-only HTML mutation, HTTP verification, automatic rollback, operator override and forced rollback. |
| Analytics | Loopback first-party collector and beacon; GA4 / Search Console / CSV importers (Google **off** by default); attribution to plan, run, deployment, versions, anchor; before/after windows with confounders; no causal claim. |
| Learning | Reflection, 12 eval suites (224 cases), offline runner, shadow, lifecycle, experiments, governed promotion. |
| Control plane | 40 server-rendered screens (section 13). |

## 4. Phase status

| Phase | Subject | State | Tag |
|---|---|---|---|
| 0 / 0.5 | Foundation, contracts, harness | complete | `phase-0-complete` |
| 1 | Content inventory | complete | `phase-1-complete` |
| 2 | Content intelligence | complete | `phase-2-complete` |
| 3 | Recommendation engine | complete | `phase-3-complete` |
| 4 | LangGraph agent review | complete | `phase-4-complete` |
| 5 | Reviewer control plane + local auth | complete | `phase-5-complete` |
| 6 | Controlled deployment | complete | `phase-6-complete` |
| 7 | Analytics | complete | `phase-7-complete` |
| 8 | Learning and evaluation | complete in the final pass (shadow only; canary/experiment deferred) | none |
| 9 | Selective autonomy | complete in the final pass, reframed by ADR-0018 | none |
| Final | Qdrant convergence, autonomy-first, demo, reviews | complete | `local-product-complete` |

Phases 8 and 9 were delivered inside the final completion pass rather than as separately tagged
phases, so there is no `phase-8-complete` or `phase-9-complete` tag; the history shows them as commits.

## 5. Database architecture

PostgreSQL 16 is canonical and holds everything, including the embedding **values**. Migrations
`0001`–`0011`:

| Migration | Adds |
|---|---|
| 0001–0003 | events; inventory (pages, versions, links, runs); content intelligence (analyses, embeddings as `real[]`, relationships) |
| 0004–0005 | recommendations (plans, candidates, transitions, opportunities); agent runs |
| 0006 | users, sessions, review actions, protection history guard |
| 0007 | controlled deployment; link-plan status integrity (deferred trigger on a `txid`) |
| 0008 | analytics (import ledger, link events, daily metrics, measurements) with writer-gated deletes |
| 0009 | learning (proposals, transitions, candidates, skills, evaluations, experiments) with the promotion guards |
| 0010 | pgvector → `real[]` (guarded, lossless), graph metrics, autonomy decisions, publish attempts, experiment INSERT guard |
| 0011 | skill promotion requires the promoter setting and a passing evaluation belonging to that skill |

Integrity is enforced in the database, not only in code: append-only tables, a same-transaction
transition record for every status change, human-only CHECKs on promotion, a 20% cap on a holdout, and
no decision row for `NO_ACTION`. The round trip `head → base → head` and `alembic check` pass.

**pgvector was removed.** Migration 0003 was amended and 0010's conversion is guarded by the live
column type. It was proven lossless on a clone of the real dev database (67 vectors, identical
fingerprint through upgrade, downgrade and re-upgrade). The extension has been dropped from the dev
database after proving nothing depended on it. The container image is still `pgvector/pgvector:pg16`
(a superset of `postgres:16`); the extension is never created.

## 6. Qdrant architecture (ADR-0017, `docs/QDRANT_ARCHITECTURE.md`)

Qdrant holds only derived points (vector, page, version, chunk, content hash, model and version). The
point id is a deterministic `uuid5`, so re-indexing overwrites. **Freshness is decided in
PostgreSQL**: a hit is stale if its page is gone, its version is not the current one, or its content
hash differs; stale hits are dropped and a stale-vector block code exists. `make qdrant-index`,
`qdrant-rebuild` and `qdrant-verify` index idempotently, rebuild from PostgreSQL and report drift in
both directions (orphaned and missing vectors). A real-container round trip was run: index → search →
rebuild → verify gave the same hits before and after. Image `qdrant/qdrant:v1.19.1`, bound to
127.0.0.1.

**Update (hardening pass):** `KEN_VECTOR_STORE` now defaults to `qdrant` in code; the in-memory store is
selected explicitly by the test factories and the offline eval benchmarks only.

## 7. Agent architecture

LangGraph is used only for agent orchestration. The critic node reviews engine-proposed plans through
a validated Pydantic boundary and hands accepted ones on (`proposed → critiqued → policy_checked`). It
never inserts, edits, approves, rejects, expires or publishes a plan, and cannot override a
deterministic block. Page text, anchor text and review notes are treated as untrusted: they are sealed
in a single JSON value with opaque references, and injected instructions were proven inert. The
LangSmith/LangChain tracing guard refuses any telemetry variable. Models are addressed by role
(`FAST`, `REASONING`), never by name; the mock provider is the default in tests.

## 8. Autonomy architecture (ADR-0018, `docs/AUTONOMY_ENGINE.md`)

Decisions: `BLOCK`, `AUTO_PUBLISH`, `AUTO_PUBLISH_WITH_EXTRA_VERIFICATION`. Modes: `shadow` (default),
`guarded`, `autonomous`.

- **15 deterministic block codes** are computed with no model call (an AST test follows the import
  graph); nothing a model returns can clear one.
- **Thresholds live in `policies/autonomy/autonomy.v1.yaml`** with contract-level floors the file
  cannot go below (guarded 0.75, autonomous 0.50, at most 5 attempts, cool-down ≥ 600 s). They were
  not tuned to the demo corpus; the shipped guarded floor (0.85) publishes nothing on it.
- **A guarded deferral is recorded as `blocked`** with an empty block list and a readable reason; an
  operator can still publish-override it. A fired block code is never overridable.
- **Configuration gates:** `autonomous` needs `KEN_AUTONOMY_AUTONOMOUS_ACKNOWLEDGED=true`; any
  publishing mode against a write-enabled HTTP CMS also needs `KEN_AUTONOMY_ALLOW_PRODUCTION=true`.
  The same rule is re-asserted before every CMS call (defence in depth). Legacy `AUTO_APPROVE` is
  still refused.
- **Audit identity:** autonomous deployments act as a named operator account (`AS_USER`) because the
  deployments table requires a `user:` actor. They are distinguishable through `publish_attempts`, the
  deployment note and the plan's transitions.

## 9. CMS safety architecture (ADR-0014)

`VALIDATE → policy → autonomy decision → dry-run → expected-version check → apply → verify → audit →
rollback`. Mutation is a **splice**: the output equals the input except the inserted `<a>` and `</a>`,
and `remove(insert(x)) == x` byte for byte. A 9,000-document differential fuzz and 37 adversarial
cases found no unintended change; a `<script>` double-escape parser differential is refused. Verified
by HTTP re-fetch; an automatic rollback restores the page byte-identically; a retry budget and
cool-down per (source, target) stop repeated unsafe retries. The HTTP CMS adapter is refused unless the
URL is loopback or explicitly allowed, and writes need their own flag. **No real CMS was used.**

## 10. Analytics architecture (ADR-0015)

A separate loopback collector (never part of the control API) records impressions and clicks as eight
pseudonymous columns — no IP, user agent, page-view id, referrer or cookie — with minute-truncated
timestamps enforced by a database CHECK. Every rejection returns the same 204. GA4 and Search Console
calls are refused before any request unless explicitly enabled, over https to the exact hosts, with a
read-only token; only a property id and dates are ever sent. Imports replace by range in one
transaction, refuse a synthetic/real mix, and refuse an import that read rows but kept none (so a
wrong file cannot wipe a range). Attribution runs from the plan to the run, deployment, versions and
anchor, with the live window taken from the plan's own transitions. **First-party counts are
spoofable** and may only ever *block* a gate.

## 11. Learning and evaluation architecture (ADR-0016)

Three trust tiers: editorial feedback is trustworthy and the only stream that can pass a gate;
behavioural is advisory and may only block; search is diagnostic. Reflection is a deterministic job;
a model, if used, only *chooses and explains* among options code enumerated, with a prompt containing
only enum codes and numbers. Frozen policy paths (placement safety, protections, the analytics and
autonomy policies, model names) cannot be proposed for change. **12 eval suites, 224 cases**, six of
them hard-fail and non-waivable (protected content, stale version, proposal safety, reflection
grounding, rollback correctness, autonomy classification); mutation tests confirm they go red when the
production guard is removed. Policy stages are all defined; **shadow** has machinery, canary and
experiment are deferred. **Nothing can be promoted** while `KEN_LEARNING_DEMONSTRATION_MODE` is on
(the default), and the database independently refuses a promotion without a human actor, an
evaluation reference, the same-transaction transition record and the promoter setting.

## 12. Interlinking pipeline (`docs/INTERLINKING_ENGINE.md`)

Inventory → intelligence → six generators → deterministic filters → ranking → anchor selection →
placement → critic → policy → autonomy → mutation → verification → analytics → learning. The five
reference repositories named in the brief are **conceptual influence only** — nothing was copied,
vendored or depended on (verified by search). Two of them cannot legally be reused as code
(CC BY-NC 4.0 and no licence), recorded in `docs/research/INTERLINKING_REFERENCE_REPOS.md`.

## 13. Frontend / control plane

Server-rendered Next.js; primary states are proposed, blocked, auto-published, verification-pending,
verified, rolled-back and failed. Screens: dashboard, autonomy operations, decision log, blocked
actions with an investigation view (why it was blocked, hard block vs deferral, override), publish
attempts and rollbacks, deployments, recommendations (score breakdown, anchor rationale),
inventory, intelligence, **graph and clusters and orphan pages**, analytics, learning (proposals,
skills, evaluations), policies, audit, system health (Qdrant, Ollama, CMS). The autonomy mode is in
the root layout of every page, and a shadow decision is labelled so it cannot be mistaken for an
action taken.

## 14. Electron-readiness

Unchanged since ADR-0008. `apps/web` stays canonical; `apps/desktop` holds only a README; no React
component imports Electron; there is a typed platform bridge as documentation; FastAPI is the only
boundary and nothing in the frontend touches a database. Electron is **not installed**.

## 15. Dependencies and licences

Added since Phase 5: `qdrant-client==1.19.1` (Apache-2.0) and its transitive packages — `grpcio`
(Apache-2.0), `protobuf` (BSD-3), `numpy` (BSD-3 plus 0BSD, MIT, Zlib, CC0), `portalocker` (BSD-3),
`h2`/`hpack`/`hyperframe` (MIT). Server image Qdrant (Apache-2.0). No Neo4j, Redis, Kafka, Celery,
Temporal or paid API. All direct dependencies were re-verified permissive. **Not audited:** licences
of libraries bundled inside binary wheels; the non-editable wheel build.

## 16. Test results

**Final gate, on the committed tree (`make verify`, fresh database, Qdrant up, host kept awake):**

| Check | Result |
|---|---|
| Ruff (lint + format) | clean |
| mypy | clean, 525 source files |
| Python tests | **6,306 passed, 0 failed, 0 skipped**; 5 deselected (the opt-in live-Ollama tests) |
| Web tests (vitest) | **1,544 passed**, 51 files; web lint and typecheck clean; production build succeeds |
| Docker Compose validation | all three profiles valid |
| Migrations | at head `0011_governance_hardening`; upgrade → downgrade → upgrade round trip and `alembic check` pass |
| OpenAPI and generated TypeScript | snapshot test passes (no drift) |
| Electron / portability guards | web guard tests pass |
| `make doctor` | **0 FAIL, 0 WARN**, 23 checks |
| Eval suites | 12 suites, 224 cases, all pass; the gate is `inconclusive` (classes below the 30-sample floor), by design |
| `make demo` | exits 0; nine acceptance checks plus a pipeline-integrity check pass; two runs give 352 identical normalised lines |

Wall time was about 16 minutes, so no host sleep interfered.

**The gate was not green the first two times, and what it found is part of the result:**

| Run | Result | Cause |
|---|---|---|
| 1 | 6,203 passed, 2 failed | A hard-coded function list in a schema test missed a guard I had added (my gap); and a 15-minute account-lockout test failed while the laptop slept for about two hours mid-run (gate wall time 4 h 08 m for 716 s of test time). It passed 6 of 6 uninterrupted. |
| 2 | 6,301 passed, 5 failed | `test_an_ended_holdout_locks_nobody` was flaky by construction (random page ids against a 20% hash-based control arm, so 0.5% per test, shared by several tests; I had wrongly called it "unexplained" earlier), and the demo's integration test leaked a cached logger that blinded four later collector tests. Both reproduced and fixed at the cause. |
| 3 | **6,306 passed, 0 failed** | — |

Two earlier hand-offs from specialist agents were also re-verified independently before being
accepted (the autonomy suites, 327 and 987 tests; the QA-fix batch, 251 tests; the web app, 1,544
tests plus a production build with a scan of the built files for secrets).

**Tests that were changed rather than left alone** (none was weakened): the tests and eval cases that
encoded the old "an uncertain critic raises the tier" behaviour; the approval-first UI tests; the
function and trigger enumerations when migrations added guards; one eval positive-control confidence
(0.8 → 0.9) because the guarded floor became 0.85; and the holdout population (24 → 64 pages) with a
threshold made relative to it.

## 17. Final architecture review

Result: **approved with changes, no blockers.** Eleven checks were put to the reviewer and answered
with file and line evidence: PostgreSQL canonical (holds), no competing pgvector path (holds, with a
caveat that the static test scans only Python), deterministic safety outside LLM control (holds),
no-approval publishing implemented safely (holds with caveat: the production gate was a single
startup validator), governed promotion (holds with caveat: guards are tamper-evident, not
tamper-proof), no forbidden dependencies (holds), FastAPI boundary and Electron readiness (holds),
layering (holds with two documented exceptions), and operational honesty (holds with caveat). Its
recommendations were acted on: the stale `awaiting_approval` text in the plan-status contract was
corrected, the operator-facing "what each mode does" sentence was added, and the production gate is
now one shared function asserted before every CMS call. Not acted on: an "autonomous" tag in the
deployment history (recorded in NEXT_STEPS), and renaming `treatment_fraction`.

## 18. Final QA/security review

Result: **pass with required fixes**; no bypass of a deterministic block, shadow mode, or the
`uncertain`-critic rule was found. Probed by execution: every API route (68 method/path pairs)
anonymously and as each role; 833 hostile query-parameter requests (no 5xx, tables intact); 13
hostile collector payloads; Google egress with the flag off (0 transport calls); concurrent
publishes (exactly one CMS write); 13 malformed-HTML cases; the holdout fraction (10.0% / 20.3% over
20,000 ids); raw-SQL attempts to change plan status. Required fixes, all done and re-verified:

| Finding | Resolution |
|---|---|
| Override recorded `critic_outcome='accept'` for a critic-rejected plan | Records the real critic outcome and verdict; refuses a `no_action` plan |
| Crawler decompression bomb (a 1 MB body inflating to 1 GiB) | Raw read, non-identity `Content-Encoding` refused; peak growth 151.0 MB → 2.7 MB |
| A skill could be promoted by raw SQL with a failing evaluation | Migration 0011 |
| Runtime accepted a database URL on port 5432 | Refused in every environment |
| IPv6 rate-limit rotation within a /64 | Keyed on the /64 |
| Publishing gate was a single startup validator | One shared function, asserted before every CMS call |

Items from this review were closed in the hardening pass (see section 26): the runtime-role `TRUNCATE`
exposure (role built, rollout pending), the crawler private-range check and the audit tag. Two
unexecuted read-only suspicions remain accepted (NEXT_STEPS).
**Not verified by the reviewer:** real Qdrant orphan/rebuild behaviour (covered by the agent's own
round trip and the demo), a stale CMS version conflict and a broken redirect, the retry budget under
a hostile loop, and the frontend in a real browser.

## 19. Remaining known risks

- Quality on real content is unproven: template parity between the inventory and a real CMS body, link
  placement naturalness on real pages, and a live model's critic quality have never been measured.
- With no real review history, calibration cannot start; the system is conservative by construction,
  and `autonomous` will publish uncalibrated plans.
- The application runs as `ken_app` on the dev database (rolled out after the hardening tag); the owner `ken` remains the trust root and is still used by migrations, `make demo`, the simulated CMS and the eval benchmarks.
- The SSRF connected-address check runs after the request is sent (one blind GET to a rebinding host is possible; the response is discarded).
- The repository sits in `~/Documents`; file-sync daemons were observed at 90–160% CPU during test
  runs, slowing them 3–10x. Not changed.
- Commit `9f634a7` is labelled docs-only but contains 44 files and 18,759 inserted lines of the Phase 8 contract freeze
  (a blanket `git add -A` while a specialist was mid-task). History was deliberately not rewritten;
  the next commit discloses it.
- `CLAUDE.md` rules 3 and 5 were reconciled with ADR-0017 in the hardening pass, on your explicit instruction.

## 20. Remaining external production requirements

| Input | Level |
|---|---|
| Real CMS: API base URL, link-change contract (409 for version conflict vs reused operation id), whether its sanitiser keeps `data-*`, **write credentials** | **ASK** |
| Authoritative content source, crawl base URL and permission, scope and protected pages, Ken's taxonomy | **ASK** / data |
| A sample of real page HTML to confirm block-hash parity | data |
| Turning on `guarded` / `autonomous`; the operator account for autonomous writes | **ASK** |
| A least-privilege (non-owner) database role | **ASK** |
| GA4 property + time zone, Search Console property, a read-only token; consent decision for the beacon; where first-party collection lives | **ASK** / legal |
| Browser verification (Playwright was deferred, amending TRD §2 / PRD §7.11) | decision |
| Your own login accounts (`make user-create`; none was created for you) | action |
| Enabling holdout experiments, turning off demonstration mode, hot policy reload, the Search Console query import | **ASK** |

## 21. What is *not* proven

All end-to-end evidence is against the simulated CMS. Not proven: behaviour against a real CMS or real
templates; a live model's quality (the suite and demo use a deterministic mock); real traffic; real
review history; the frontend in a real browser; Google API behaviour; a non-editable wheel build.

## 22. Exact local startup steps

```bash
colima start                 # if Docker is not running
make setup                   # .venv (Python 3.12), dependencies, .env from .env.example
make dev                     # Postgres :5433 + Qdrant :6333, then migrations
make dev-api                 # terminal 1: FastAPI on http://127.0.0.1:8000
make dev-web                 # terminal 2: Next.js  on http://127.0.0.1:3000
make user-create USERNAME=<you> ROLE=operator_admin DISPLAY_NAME="<You>"   # you choose the password
make doctor                  # environment matrix; expects 0 FAIL
```

A real run of the pipeline: `make ingest-file FILE=<export.jsonl>` → `make analyze` →
`make qdrant-index` → `make graph-metrics` → `make recommend` → `make agents` →
`make autonomy-dry-run` (prints, records nothing) → `make autonomy` (records decisions) →
`make autonomy-run AS_USER=<operator>` (publishes **only** if the mode and acknowledgements allow).
The default mode is `shadow`, so nothing publishes until you change it deliberately.

## 23. Exact demo steps (`docs/DEMO.md`)

```bash
make db-up qdrant-up         # once
make demo                    # about 22 seconds; exits 0 only if all nine acceptance checks pass
make demo-clean              # drops ken_demo, ken_cms_sim_demo and the ken_demo_pages collection
```

It opens with a banner that all data is synthetic and ends with what it does not prove. To browse the
resulting database in the UI, set `KEN_DEMO_ADMIN_PASSWORD` (your own value) before `make demo`.
Plain `AUTO_PUBLISH` appears only after a **clearly labelled synthetic calibration fixture**, shown
before and after.

## 24. Exact production-integration prerequisites

1. Decide the CMS contract and obtain write credentials (section 20); run `make cms-sim-seed --export`
   on real HTML to confirm block-hash parity before any write.
2. Run `KEN_CMS_PROVIDER=http` with `KEN_CMS_ALLOW_REMOTE` and `KEN_CMS_WRITES_ENABLED` set only
   deliberately, against a staging CMS first.
3. Move the application to a non-owner database role; keep migrations on the owner.
4. Keep `shadow` until you have read the decision log on real content; move to `guarded` only after a
   decision class has real calibration history; treat `autonomous` as the step that starts changing a
   live site.
5. Run on Qdrant (`KEN_VECTOR_STORE=qdrant`) and watch `make doctor`.
6. Decide the analytics and consent questions before putting the beacon on a real page.

## 26. Post-completion hardening pass

Scope: a focused hardening pass, not a rebuild. Shadow mode stays the default; nothing enables live
autonomous publishing; nothing was pushed or deployed.

| Objective | Result |
|---|---|
| CLAUDE.md / Qdrant | CLAUDE.md rules 3 and 5, TRD, START_HERE, REPO_CLONE_PLAN, IMPLEMENTATION_PLAN, MULTI_AGENT_ORCHESTRATION reconciled (PostgreSQL = truth, self-hosted Qdrant = approved derived index, memory store = tests only). Code default is `qdrant`; test factories pick `memory`. I added to rule 5 the sentence "Neo4j, Redis, Kafka, Celery and Temporal remain forbidden" - from your earlier directives, not from an ADR; strike it if unwanted. |
| SSRF | Shared `ken_core/net/guard.py` for the crawler and the verification probe: all-record DNS check, address classes above, hostnames, odd literals, per-hop redirect checks, connected-peer check; fixture escape hatch `KEN_CRAWL_ALLOW_PRIVATE_NETWORKS` (loopback only; refused outside local/ci). 95 guard/fetcher tests. Behaviour change: https to http redirects are now refused. |
| DB roles | `ken_app` + `KEN_MIGRATION_DATABASE_URL`, `make db-roles`/`db-roles-check`, doctor check; 26 tests as `ken_app` and the autonomy, deployment, learning, review, analytics, vector and graph suites run through it. Rollout to the dev DB is yours (NEXT_STEPS 28). |
| Lockout test | Injected fake clock; asserts held at +14m59s and released after. |
| Audit tag | `autonomous`, `autonomy_decision`, `autonomy_decision_id`, `autonomy_mode`, `autonomy_rolled_back` on deployment views, UI badge, OpenAPI/TS regenerated, 5 backend and 2 web tests. |
| Live vs mock | `qdrant` marker + `KEN_REQUIRE_QDRANT_TESTS`; `make test-all`/`verify` fail if Qdrant is down. Live Ollama (`make test-live`) ran once: 5 passed. |

Final gate (fresh DB, caffeinate, Qdrant up): ruff and mypy clean; **6,413 Python passed, 26 skipped**
(the `ken_app` tests, which need `KEN_TEST_APP_DATABASE_URL`, and were run separately: 26 passed), 5 live-Ollama
deselected; **1,546 web tests passed**; doctor 0 FAIL, 3 WARN (role not yet rolled out; Qdrant collection
not indexed in the dev environment). Independent review: no critical findings; the high findings (eval
benchmarks writing to the shared Qdrant index, `.env` permissions, stale docs) were fixed.

### 26.1 Role rollout (follow-up)

Rolled out `ken_app` on `ken_interlink` (port 5433 only). Before: `pg_dump -Fc` backup (`~/.ken-backups/`,
mode 0600, with a copy of `.env`) proven restorable into a throwaway database (same revision, 64 pages),
then dropped. Server logging checked: `log_statement=none`, but `log_min_error_statement=error` would
log a failing `ALTER ROLE ... PASSWORD`, so the script now sends a SCRAM-SHA-256 verifier instead of the
plaintext (tests added); server logs contain no password text. After: `make db-roles-check` OK, doctor
0 FAIL / 1 WARN (Qdrant collection not indexed), `/ready` 200 as `ken_app`, eight privileged statements
denied with 42501. `.env` stays untracked at mode 0600. Not re-run: the full suite (no application or
SQL grant code changed; the earlier full gate and the 26 role tests stand).

## 25. Git state

- **Tag:** `local-product-complete` (local only). The commit it points at is the one that adds this
  report; `git log -1 local-product-complete` shows it. The working tree is clean at the tag.
- **Nothing was pushed.** No remote is configured (`git remote -v` is empty), so a push is not even
  possible until you add one. No pull request was created.
- **History was not rewritten.** Three commits are labelled `wip:` and say in their messages that they
  are unverified checkpoints, taken after an agent batch was cut off by a usage limit or lost its
  connection: `fb6046c`, `a434248`, `81a137f`. Each was followed by a commit that verified the work.
- **A mislabelled commit:** `9f634a7` is titled as a docs-only change but contains 44 files and
  18,759 inserted lines (the first tranche of the Phase 8 contract freeze), because a blanket
  `git add -A` ran while a specialist was mid-task. The next commit (`295b94b`) discloses it.
  Rewriting history needs your approval, so it was left alone.
- **21 commits** separate `phase-7-complete` from the final tag.
- **The model was changed mid-session** (Opus 5 to Sonnet 5.5); the commit trailers name whichever
  model wrote each commit.
