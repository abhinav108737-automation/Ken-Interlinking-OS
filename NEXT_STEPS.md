# Next Steps

**Current state: the local product is complete and ready for production integration.** Phases 0-9 are
built, tested and reviewed; the full gate (ruff, mypy, Python and web tests, compose, migrations,
`make doctor`) passes on a freshly created database. Nothing has been pushed, deployed, or pointed at
a real CMS, site or Google service. See [FINAL_PROJECT_REPORT.md](FINAL_PROJECT_REPORT.md), and the
per-phase reports [PHASE_7_REPORT.md](PHASE_7_REPORT.md), [PHASE_6_REPORT.md](PHASE_6_REPORT.md),
[PHASE_5_REPORT.md](PHASE_5_REPORT.md), [PHASE_4_REPORT.md](PHASE_4_REPORT.md),
[PHASE_3_REPORT.md](PHASE_3_REPORT.md), [PHASE_2_REPORT.md](PHASE_2_REPORT.md),
[PHASE_1_REPORT.md](PHASE_1_REPORT.md) and [PHASE_0_REPORT.md](PHASE_0_REPORT.md).

**What "ready for production integration" means here, and what it does not.** The system runs end to end
against a simulated CMS and a synthetic corpus (`make demo`). It has never touched a real CMS, a real
Ken Research page, a real analytics property, or real review history. Every statement about quality
on real content is therefore unproven. The default mode is `shadow`, which publishes nothing.

**Execution mode:** the user authorised autonomous execution of Phases 2-9, the end-to-end demo and
the final report. Execution stopped only for ASK/BLOCK actions. Everything below that is marked ASK
is still waiting on a human decision.

## Needed from you before real production ingestion

The inventory works end to end on fixtures, exports and a mock CMS. Ingesting real Ken Research
content needs **one** of the three sources below, plus a few decisions. No production URL appears in
any governing document, so none was guessed.

| # | What | Why | Permission level |
|---|---|---|---|
| 1 | **Which source is authoritative:** the public website, a CMS read API, or a CMS export | One authoritative source per site avoids version churn between sources (ADR-0009) | decision |
| 2a | *If the public site:* the exact base URL (`KEN_CRAWL_BASE_URL`), any extra hosts (e.g. the `www.` variant, `KEN_CRAWL_ALLOWED_HOSTS`), and confirmation that crawling it is permitted | The crawler has no default site and refuses to start without one | **ASK** — crawling a domain needs your approval |
| 2b | *If the public site:* acceptable politeness settings and timing: delay (default 1 s), concurrency (2), page cap (500), and any crawl window or a contact URL for the user-agent string | Load on the production site | decision |
| 3a | *If a CMS read API:* its list and get endpoints, pagination, page-id stability, how drafts and publication state are exposed, and the field that holds the page template/type | `CMSContentSource` is complete; only a real `CMSInventoryReader` is missing | — |
| 3b | *If a CMS read API:* **read-only, least-privilege** credentials (TRD §23) and where they are stored | Never in source control | **ASK** |
| 4 | *If an export:* a sample export, so it can be mapped to the documented JSONL format (`docs/CONTRACTS.md`) or given its own adapter | Offline bootstrap with no load on the live site | — |
| 5 | **Scope:** which sections are in scope (e.g. only `/industry-reports/…`), which are excluded, and which pages are **protected** (never to be modified later) | Phase 3 eligibility and Phase 6 safety depend on it | decision |
| 6 | Is the site multilingual? | Language comes only from markup and headers (no detection library) | decision |
| 7 | **Ken's real taxonomy**: markets/industries with aliases, geographies, a company/product gazetteer, page-type URL patterns, and protected-page rules | `policies/taxonomy/taxonomy.v1.yaml` is an illustrative seed. Classification quality on the real site depends on this. | data |
| 9 | **Self-describing anchors (ADR-0011 §9.1):** should a same-market, cross-country pair (India cold-chain report → UAE cold-chain report) be linkable on the plain market name ("cold chain")? Today: only when the block also names the target's country/region, or on an article → report hop. 45 eval pairs were relabelled ACCEPTABLE accordingly (rule R5). | Editorial policy; one policy switch either way | decision (orchestrator default in place) |
| 11 | **Create your own accounts.** No user exists after install, and none was created for you: run `make user-create USERNAME=… ROLE=operator_admin`, then reviewers/viewers as needed. | The UI and API require a session from Phase 5 on | action (you) |
| 10 | ~~Does a model critic's `reject` filter a plan out of the human queue?~~ **Superseded by ADR-0018.** There is no human queue in the normal flow. A critic `reject` blocks the plan (recorded `blocked`), and an operator may still publish-override it with the real critic outcome recorded. A critic `uncertain` or failed answer is `NO_ACTION` and never a publish (ADR-0019). | — | closed |
| 12 | **Real CMS integration (ASK).** The CMS API base URL; its link-change contract, which must tell a version conflict (409) apart from a reused operation id (ADR-0014 §9); whether its sanitiser keeps `data-*` attributes; and **write credentials** stored outside source control. Then `KEN_CMS_PROVIDER=http`, `KEN_CMS_ALLOW_REMOTE=true` and `KEN_CMS_WRITES_ENABLED=true`, all with your approval. | Phase 6 runs only against the mock and the simulated CMS | **ASK** |
| 13 | **Browser verification (amends TRD §2 / PRD §7.11).** Playwright was **not** adopted: verification parses the served HTML deterministically (ADR-0014 §6). Confirm the deferral, or ask for a headless-browser check for client-rendered pages. | It avoids a large browser download and process | decision |
| 14 | **Real-template parity.** Run `make cms-sim-seed --export` on a sample of Ken's real HTML, or give us 10–20 real page bodies, to confirm that block hashes match between the inventory and the CMS body. Where parity breaks, the mutator refuses; it never guesses. | It proves deployment works on real templates | data |
| 15 | **Real analytics (ASK).** A GA4 property id and its time zone, a Search Console property (`sc-domain:` or a URL prefix), and a **read-only** OAuth token plus how it is refreshed. Then `KEN_ANALYTICS_GOOGLE_ENABLED=true`, with your approval. | GA4/GSC calls are external egress; they stay refused before any request until you enable them | **ASK** |
| 16 | **GA4 setup, only if you want link-level GA4 data.** Register the event-scoped custom dimension `ken_link_id` and forward the beacon's two events (`internal_link_impression`, `internal_link_click`). | GA4 only accrues custom-dimension data from registration onward, and high cardinality can produce `(other)` rows | action (you) |
| 17 | **Consent decision for the beacon (ASK / legal).** It sends behavioural events and sets no cookie, but ePrivacy, GDPR and India's DPDP Act may still treat it as analytics requiring consent. It reads `window.kenAnalyticsConsent` and honours GPC and DNT. Putting it (or the collector) on the real site is a live deployment. | Serving it from the real site needs your consent-manager choice | **ASK** |
| 18 | **Where production first-party collection lives.** Until you decide, the aggregated daily CSV (`ken_link_daily`) is the import contract and the collector stays loopback-only. | The local collector is a demo and a reference implementation, not a production service | decision |
| 8 | **PRD v1.1 §17.1 wording:** "a genuine native degraded mode" now holds only for schema ≤ 0002 (pgvector is required from Phase 2, ADR-0010) | A product-document revision, for your approval | decision |

| 19 | **Hot policy reload (Phase 8, ADR-0016 §16).** Promotion writes a new policy file and changes the ACTIVE policy path, and `Settings` is frozen, so a promotion needs an operator restart. Hot reload would be a material architecture change. It is also moot while promotion is blocked. | Operability of promotion once it is unblocked | decision (**ASK** — architecture change) |
| 20 | **Turning off learning demonstration mode (Phase 8, ADR-0016 §16.1).** `KEN_LEARNING_DEMONSTRATION_MODE` defaults `true` and refuses every promotion, so Phase 8 exits with zero promotions by construction. The corpus is ~65 **synthetic** pages, 48 plans and scripted review actions: the one stream the design calls trustworthy — human editorial decisions — is the stream with no real data. Real editorial volume is the gate on learning being more than plumbing. | Nothing on synthetic data should be promoted | decision (**ASK**) |
| 21 | **Enabling holdout experiments (Phase 8).** `KEN_EXPERIMENTS_ALLOW_HOLDOUT` defaults `false`. A holdout deliberately WITHHOLDS inbound links from its control **target** pages, a real editorial and SEO cost on the live site. The share is capped at 20 % by a database CHECK (policy default 10 %), and while the setting is off the lifecycle service refuses activation AND withholds the transaction-local `ken.learning_experiment_activator` setting migration 0009's guard requires — so no code path, not even a raw UPDATE, can start one. `policy_ab` (both arms get links) is unaffected. | Withholding links from real content is your decision | decision (**ASK** to enable) |
| 22 | **The deferred Search Console query import** (`page_query_metrics_daily`, `--queries`), without which search-side learning has no data at all. It is Google egress. | Search signals stay diagnostic-only and currently empty | **ASK** |
| 23 | **`review_performance` (ADR-0012 §42) is carried to Phase 9**, not implemented in Phase 8. The reviewer-performance view it reserves for "Phases 6–8" needs editorial volume that the synthetic corpus does not have; it is recorded here rather than left silent. | Avoids a silent gap against ADR-0012 | decision (orchestrator default: defer) |
| 24 | **Turn autonomous publishing on, or not (ASK).** The default is `shadow`, which publishes nothing. `guarded` publishes nothing until a decision class is calibrated from real review history. `autonomous` DOES publish uncalibrated plans, with the strict verification profile. Reaching it takes three deliberate settings: `KEN_AUTONOMY_MODE=autonomous`, `KEN_AUTONOMY_AUTONOMOUS_ACKNOWLEDGED=true`, and, for a write-enabled HTTP CMS, `KEN_AUTONOMY_ALLOW_PRODUCTION=true`. Nobody has set them. | This is the decision that starts changing a live site | **ASK** |
| 25 | **A named operator account for autonomous writes.** `deployments.actor` must be a `user:` account, so the autonomous run acts as one (`make autonomy-run AS_USER=<name>`). Choose or create it (`make user-create`); it will appear as the actor on autonomous deployments. An auditor tells them apart from human deployments through `publish_attempts` and the deployment note (see `docs/AUTONOMY_ENGINE.md`). | Audit identity | action (you) |
| 26 | **Real review history before any calibration.** Plain `AUTO_PUBLISH` needs a calibrated decision class: a passing, fresh evaluation with at least 30 samples for that exact class. That cannot exist until reviewers (or operators acting on exceptions) have produced real decisions on real content. Until then every plan gets extra verification or a block. | The system is conservative by construction until it has evidence | informational |
| 27 | **Run on Qdrant, not the in-memory index.** The code default is `KEN_VECTOR_STORE=qdrant` (the test factories select `memory` explicitly); `.env.example` also sets it. A production installation must run `make qdrant-up`, `make qdrant-index` and keep `KEN_VECTOR_STORE=qdrant`. `make doctor` warns in staging/production if it is not. | Without it the semantic channel has no index to search | action (you) |
| 28 | ~~Roll out the least-privilege role.~~ **Done (2026-10-24).** `ken_interlink` now runs as `ken_app`: `KEN_DATABASE_URL` is the app DSN and `KEN_MIGRATION_DATABASE_URL` the owner `ken` DSN in the (gitignored, mode 0600) `.env`. Verified: `make db-roles-check` OK, `make doctor` role check OK, `/ready` 200 as `ken_app`, and TRUNCATE, CREATE/ALTER/DROP, `DISABLE TRIGGER`, `SET session_replication_role`, `ALTER ... OWNER` and `CREATE ROLE` all return 42501. Still true: the owner (`ken`, a superuser in the dev container) is the trust root; `make demo`, the simulated CMS and the eval benchmarks keep using it; the `ken.*` promotion/activation settings remain a code-path control. Rollback: restore `.env` from `~/.ken-backups/env-*.bak`, or `DROP OWNED BY ken_app; DROP ROLE ken_app;` as `ken`. | Residual: the owner credential | done |
## Browser-rendered crawling (2026-10-26)

The pilot crawler can render pages with Playwright/Chromium (`render: browser`, both pilot files; setup and limits in `docs/pilot/KEN_PUBLIC_CRAWL_PILOT.md` section 2a). The pilot configuration hash changed (now `7c9abb1a...`), so any earlier approval file is invalid; the live robots check and the crawl still need your approval file. **Incident to know about:** during development, two unit tests accidentally drove the pilot configuration with a mock transport that browser mode ignored, so Chromium sent real page requests to www.kenresearch.com (about a minute, 3 s apart, honest user-agent, real robots.txt NOT read, nothing stored). Fixed and regression-tested; tell the technology team if they ask about unexpected traffic from this machine around 2026-10-26 evening.

## Offline interlinking demo (2026-10-26) - synthetic only

See `docs/pilot/OFFLINE_INTERLINKING_DEMO.md` (commands), `docs/pilot/MANAGEMENT_DEMO_GUIDE.md` (guide and the evidence still needed) and the committed report `docs/pilot/offline_demo_report/`. `make offline-demo ARGS=--fresh` (about 12 min) or `make offline-demo-fast`. It ran the full shadow pipeline on 75 synthetic pages: nothing crawled, published or sent anywhere. Real-content findings: **none**.

| Item | Note |
|---|---|
| Import path for authorised content | `import-html-dir` (folder) and `import-file --provenance authorized_import` (JSONL); the runner accepts a folder only with `--confirm-authorized`. Provenance labels (`synthetic_base`, `synthetic_extension`, `authorized_import`) keep synthetic and real findings apart; unlabelled pages count as neither. |
| Honest misses in the demo | A designed spoke page (SE Asia last-mile) and the Philippines EV-charging orphan received no proposal; the report shows them. Uncalibrated thresholds: 9 critic-accepted proposals were deferred by the confidence floor. |
| What is still needed | Authorised real content (the live crawl needs the technology team: `docs/pilot/KEN_PUBLIC_CRAWL_PILOT.md`), human relevance labels, template validation, baseline Search Console data, a controlled rollout with a holdout. Nothing here supports an SEO-effect claim. |
| Leftovers | Database `ken_offline_demo` and Qdrant collection `ken_offline_demo_pages` (recreated by `--fresh`). |

## Ken public crawl pilot (prepared 2026-10-26) - nothing crawled

See `docs/pilot/KEN_PUBLIC_CRAWL_PILOT.md`. Configuration: `pilots/kenresearch/pilot-25.yaml` (active once approved) and `pilot-large.yaml` (300 pages, `activated: false`). Offline command: `make pilot-preflight`. Live commands (`make pilot-robots`, `make pilot-crawl`) refuse to run without a gitignored approval file that quotes the exact configuration hash. **No live request has been sent.**

| Item | Needed from you |
|---|---|
| Authorisation | An approval file (`pilots/kenresearch/approvals/pilot-25.json`) for `live_robots_check`, then `live_crawl`. |
| Contact | `KEN_CRAWL_CONTACT`: a monitored role mailbox or public bot-information URL (preflight blocks without it). |
| Permission | Confirmation Ken Research permits the crawl and the contact string. |
| Unknowns | robots.txt unread; exclude patterns untested against the real site; www vs apex redirect unverified (apex stays unapproved). |
| Not built | A sitemap reader (deliberately absent: sitemaps cannot widen limits); a link-eligibility policy for the live site (nothing is assumed linkable). |

## Local shadow pilot (2026-10-25) - what remains

See `LOCAL_SHADOW_PILOT_REPORT.md` (corpus is SYNTHETIC `research.example.com`; nothing published).

| Item | Note |
|---|---|
| Legacy `awaiting_approval` plans | **Resolved without a rebuild.** The 33 plans in `ken_interlink` are classified `eligible` by `make autonomy-legacy` (dry run), are now shadow-evaluated (33 decision rows, statuses and history unchanged) and stay `awaiting_approval`; they can only move further by an operator act. The reconcile command expires/blocks defective legacy plans on request (`ARGS=--apply`) and never publishes. Details: `docs/pilot/SHADOW_PILOT_PERFORMANCE_REPORT.md` section 6. |
| Agent step runtime | The pilot's 2-hour figure was host sleep; the critic takes about 10.8 s per source (324 s for 30). `make agents` now uses `caffeinate -i`. Measured on a fixed labelled set, **no faster variant kept quality** (compact prompt 1.7-1.9x but -1 to -2 of 21 GOOD accepts; brevity caps 1.3x but accepted the BAD plan; concurrency, context window and pre-filtering gave ~0-2%). The production critic is unchanged. **Decided 2026-10-25:** keep `qwen3:8b` and the current prompt; `OLLAMA_NUM_PARALLEL` stays unchanged. The smaller model `qwen3:4b-q4_K_M` (Apache-2.0, 3.0 GB vs 5.6 GB, about 1.4x faster) was benchmarked and **rejected**: it accepted the one BAD-labelled plan and 10 ACCEPTABLE plans the 8B model rejects. It is still installed (`ollama rm qwen3:4b-q4_K_M` removes it). Use `caffeinate -dims`, not `-i`, for long jobs. |
| Real pilot inputs (ASK) | Approved Ken source URL or export, a 200-500 page sample with real templates, and human relevance labels (a few hundred) for precision and calibration; see report section 5. Nothing is calibrated until then. |
| Pilot leftovers | `ken_pilot` database and `ken_pilot_pages` collection (safe to drop). |

## Hardening pass (post `local-product-complete`) - what remains

| Item | Note |
|---|---|
| Autonomous deployment audit tag | Implemented without a migration: `autonomous` derives from a `publish_attempts` row by the engine actor. The decision id is a best-effort match (plan, decision, mode, latest before the attempt) because `publish_attempts` has no decision FK; a migration adding one would make it exact. It is two queries per row (N+1 on the list). The `POST /deployments` response carries no tag, only the GETs. |
| `db_roles.py` password handling | **Fixed in the rollout:** only a client-computed SCRAM-SHA-256 verifier is sent in `ALTER ROLE`, never the plaintext, so statement logging (this server has `log_min_error_statement=error`) cannot capture the password. `assert_safe_target` checks port and `ken_*` name, not the host. |
| Live-service tests | Live Ollama (5 tests, `make test-live`, opt-in) and real Qdrant (`qdrant` marker, `make test-qdrant`, fails not skips under `make test-all`) are separate from the deterministic mocks. They prove the integrations work, NOT real-content quality or calibration. |
| Qdrant default | The code default is now `qdrant`; `make_settings` selects `memory` for deterministic tests; the eval benchmarks pin `memory` so they never write to the shared index. A direct `Settings(_env_file=None)` in a new test gets `qdrant` unless it opts out. |

## Phases 8-9 and final-pass known limitations (accepted, documented)

From the final architecture review, the final QA/security review and the workstream reports.

| Item | Why it is accepted / what to do |
|---|---|
| **The DB guards are tamper-evident, not tamper-proof.** With `ken_app` applied (row 28) the runtime can no longer `TRUNCATE`, alter the schema or disable triggers, so the append-only and status-integrity triggers are privilege-enforced against it. The promoter and activator settings stay a code-path control, and `ken_app` still has row-level `DELETE` on tables with no delete trigger. The owner credential is the remaining trust root. | Hold the owner DSN only in the migration step. |
| Autonomous deployments are attributed to a `user:<AS_USER>` account in `deployments.actor`, so someone scanning only that table sees a human. The distinction is in `publish_attempts`, the deployment note and the plan's transitions. | Add an "autonomous" tag to the deployment history, derived from the `publish_attempts` join (contract, API and UI change). |
| An operator may publish-override a **critic-rejected** or **deferred** plan (the override records the real critic outcome and forces the strict profile), but never a plan with a fired block code, and never an `uncertain` plan. | Deliberate human exception; documented in `docs/AUTONOMY_ENGINE.md`. |
| A link a CMS post-processor has marked `hidden` cannot be auto-rolled-back: the inverse splice refuses with `rollback_conflict` and escalates. | Pinned by two eval cases. Needs the real CMS's behaviour. |
| A crash between the CMS write and the `publish_attempts` insert loses one attempt, leaving the budget one short. The cool-down still prevents a hot retry loop. | Documented in `publish.py`. |
| Crawler and verification-probe SSRF (hardening pass): a shared guard (`ken_core/net/guard.py`) resolves every A/AAAA record, refuses loopback, private, link-local (incl. 169.254.169.254), CGNAT, ULA, metadata hosts and mapped/6to4/NAT64 forms, re-checks every redirect hop, and checks the address the socket actually connected to. Residual: the connected-address check runs after the request is sent, so a DNS-rebinding host can still receive one blind GET (the response is discarded); pinning the connection to the checked IP would close it. Operator-configured CMS/analytics/Ollama URLs are not routed through the guard. `KEN_CRAWL_ALLOW_PRIVATE_NETWORKS` (loopback only, refused outside local/ci) exists for fixtures. Crawling a real domain remains ASK. | Pin the peer IP before a first real crawl if the content is untrusted. |
| The crawler now refuses any non-identity `Content-Encoding` (it never asks for compression). A site that sends one anyway is recorded `TOO_LARGE`, and its robots.txt is treated as disallowing the origin. | Fixed after QA; one bomb test pins the memory bound. |
| `treatment_fraction` on a **holdout** means the WITHHELD share (the control arm), not a treatment share. The column name predates the correction. | Documented in `protocol.py`; renaming needs a migration. |
| Holdout experiments exist but cannot be activated by default: the setting, the activator GUC, the INSERT guard and a 20% database cap all apply. | Withholding links from real pages stays your decision (row 21). |
| CANARY and EXPERIMENT stages are defined in the contract and schema; only SHADOW has machinery. | Deferred until production-like traffic gives them something to measure (ADR-0016 §17). |
| `ExperimentDetail.comparison` is not wired (`compare_arms` exists), `policy_shas` is always empty, and lifecycle `operation_id`s are not persisted (migration 0009 has no column). | Cosmetic for now; no experiment can run without row 21. |
| `CriticReason` has no members for duplicate intent, cannibalisation, destination fit or editorial awkwardness. | Frozen contract follow-up; the specific finding is in the step output meanwhile. |
| UI gaps: no experiments screen, no `/learning/evaluations/[id]` detail, `/dashboard` has no autonomy counts, `/autonomy/blocked` has no per-plan route or `plan_id` filter. | The screens that exist compose what the API offers. |
| `lib/platform` and the typed Electron bridge are documented comments; `apps/desktop` holds only a README. Electron is not installed. | Unchanged since ADR-0008. |
| The CMS factory resolves `ken_services.deployment.changes` lazily by name and `ken_agents/worker.py` builds a CMS provider (a Phase 0 heartbeat probe). Both are documented exceptions to the dependency direction and are invisible to a static import check. | Documented. |
| The compose image is `pgvector/pgvector:pg16` (a superset of `postgres:16`) with the extension never created; the Qdrant image is pinned by tag with its digest in a comment. | Pin the digest in the `image:` line for any shared environment. |
| The libraries bundled inside binary wheels (for example in `numpy`) were not licence-audited; package metadata was. The non-editable wheel build across the five package roots was not verified. | Before any distribution. |
| Wall-clock sensitivity: the lockout test now uses an injected clock (fixed). The remaining sleep-based tests only assert that a lock is still HELD, so a host sleep cannot fail them wrongly. The full gate still takes ~15 minutes; run it under `caffeinate`. | Keep `caffeinate` for the gate. |
| The repository sits in `~/Documents`, which cloud file-sync daemons were observed hammering (system `fileproviderd` and OneDrive's provider at 90-160% CPU), making test runs 3-10x slower than at the start. | Move the repo out of a synced folder, or exclude `.venv`, `node_modules`, `.next` and `__pycache__`. Not changed here. |
| Commit `9f634a7` is labelled docs-only but contains 44 files and 18,759 inserted lines of the Phase 8 contract freeze (`git add -A` ran while a specialist was mid-task). History was not rewritten. | Disclosed in the commit that followed it (`295b94b`). Rewriting history needs your approval. |
| The 5432 guard does not resolve DNS (`127.1`, decimal IPs). The runtime engine now refuses a native-port URL in every environment (override: `KEN_ALLOW_NATIVE_POSTGRES_PORT`). | Optional. |

## Licence hazards in the research references (standing warning)

Two of the five interlinking repositories named as research references **cannot be reused as code**,
and both are the kind of thing a future contributor would be tempted to "borrow a helper" from.
Verified 2026-10-16 (see [docs/research/INTERLINKING_REFERENCE_REPOS.md](docs/research/INTERLINKING_REFERENCE_REPOS.md)):

| Repository | Licence | Consequence |
|---|---|---|
| `searchsolved/search-solved-public-seo` | **CC BY-NC 4.0** (NonCommercial) | Unusable in this commercial project. It is also the most popular of the five, so the most tempting. **Read the published technique only.** |
| `Deepak-J0shi/SEO-Interlinking-Tool` | **none declared** | Default copyright: no permission is granted to copy, modify or distribute anything. **Read only.** |
| `martech-engineer/WebKnoGraph` | Apache-2.0 | Reusable with attribution, if ever needed. |
| `avansaber/seo-monster` | MIT | Reusable with attribution, if ever needed. |
| `andersongrowth/internal-link-mapper` | MIT | Reusable with attribution, if ever needed. |

**Current state: nothing was copied.** A repository-wide search finds no reference to any of these
projects outside that one document, no vendored directory and no dependency naming them; the
influence is conceptual only. Copying from either of the first two would introduce a licence
incompatibility, which this project treats as a hard stop.

## Hard gates carried into later phases (from the Phase 3 QA review)

| Gate | Phase |
|---|---|
| **Least-privilege DB role** in any shared or production environment: DML on application tables only, no DDL, never superuser (ADR-0014 §5). It is required for the status-integrity trigger to be a real boundary (ADR-0014 §10 I1). | production integration |

## Phase 7 known limitations (accepted, documented)

| Item | When |
|---|---|
| **First-party counts are spoofable.** Origin and the page-view id are attacker-chosen, so anyone who can reach the collector can inflate counts for a live link. Phase 9 must never graduate a decision class on first-party data alone. | binding on 9 |
| Outcomes are **correlational only**: no causal claim, no significance, no ranking or revenue attribution. Experiments (the policy EXPERIMENT stage) arrive in Phase 8. | 8 |
| `overlapping_changes` flags most batch deployments, so Phase 8 should group outcomes by deployment rather than by link | 8 |
| Cross-source totals double-count: GA4 and first-party can both see one event. Shown with a caveat wherever more than one source contributes. | informational |
| Timing still separates a stored event from a rejected one, even though the response is byte-identical | inherent |
| An all-rejected import is audited only as a failed job run (no ledger row) | informational |
| No real-browser beacon test (Playwright still deferred); GA4/GSC unproven against the live APIs by design | with item 13 / item 15 |
| A plan whose verification stays inconclusive stays `deployed` and never measures | informational |
| Retention periods are recorded in the policy but enforced by nothing (the purge command is deferred) | when real volume exists |
| The default aggregation window covers only **complete** UTC days, so events from today need an explicit range | informational |
| An aggregation that reads no events records `synthetic = false` on its ledger row by default | cosmetic |

## Phase 6 known limitations (accepted, documented)

| Item | When |
|---|---|
| Parity with Ken's real HTML templates is unproven: the demo corpus agrees across five HTML forms (ADR-0014 §9) | before real CMS integration (item 14) |
| tx1 holds the source lock and plan row locks across the CMS read, validate and target-reachability probes. This is bounded by timeouts, and the probes run sequentially. | before high-volume deployment |
| Any 409 from the HTTP CMS counts as a version conflict, so a CMS that also uses 409 for a reused operation id would expire plans | real CMS contract (item 12) |
| Rollback when an editor already removed our link counts as `rolled_back` | — |
| Verification has no overall deadline (per-read timeouts only). `inert` and `opacity:0` are not modelled as hidden. | optional |
| A sim DB URL with an empty host falls back to `PGHOST` | optional |
| No browser verification (item 13) | decision |
| `events` is still not append-only in the database, and the app still connects as the superuser `ken` locally | production integration |
| `identity_conflict` operator tooling (carried over from Phase 5) is still not built | when real data shows conflicts |

## Phase 5 known limitations (accepted, documented)

| Item | When |
|---|---|
| Per-account lockout: anyone who knows a username can lock that account for 15 minutes (accepted for a few local users) | if exposed beyond a trusted network |
| The CSP allows `'unsafe-inline'` scripts and styles (no nonces yet). A nonce-based CSP can be generated in `proxy.ts`. | before any shared deployment |
| Review forms need JavaScript (they work around React 19's form reset); login and logout work without it | — |
| `target-options` loads live facts for every page on each call: fine for thousands of pages | at scale |
| ~~Read-only DB path for GET handlers; status integrity~~ closed in Phase 6. Least-privilege role → "Hard gates". | — |
| `identity_conflict` operator tooling | see Phase 6 limitations |
| User administration emits no event: adding an event name is a governed decision | 8 / when audit of user admin is needed |

## Phase 4 known limitations (accepted, documented)

| Item | When |
|---|---|
| A plan that fails `check_invariants`, or conflicts at hand-off, gets no review row, even when the critic rejected it. A later run reviews it again, spending another model call, and may hand it off once the problem clears. This is by design, because state changed, but it is not free. | 8 (if it matters) |
| The local REASONING critic takes about 10 s per source (qwen3 8B on this Mac); a 100-source batch takes about 15 min. The deterministic fallback keeps every run correct. | at scale |
| Critic verdicts are uncalibrated, and the deterministic threshold uses raw confidence. | 8 |
| The recorded eval fixture pins confidences to ±0.001. A ranking change shows up as fixture drift: re-record, never edit expectations. | — |
| `langgraph-sdk` caps `websockets` below 17 (16.1.1 is locked). | on LangGraph upgrades |
| The agents API has no endpoint that starts a run (CLI only); decided in Phase 5 to keep it so (ADR-0013 §7). | — |

## Phase 3 known limitations (accepted, documented)

| Item | When |
|---|---|
| The semantic channel is an exact KNN scan per source, O(N²) per run; the graph channel runs a correlated inbound-count subquery per candidate (`world.inbound` already holds the counts). Fine for the demo; add an HNSW/ivfflat index (pgvector, no new service). | at scale |
| `recommendation.v1` and migration 0004 were amended in place before release (ADR-0011 §9.8); both are immutable from the `phase-3-complete` tag. Any local DB at 0004 from before the tag must be rebuilt (`downgrade 0003_content_intelligence`, `upgrade head`). | — |
| Channel ablations barely move the metrics on the 65-page demo (k = 30 is nearly exhaustive); placement is the binding constraint. Re-measure on Ken's content before re-weighting. | 8 |
| Raw confidence only (`calibration_version = null`). | 8 |
| Adding `page_versions.search_vector` rewrites the table under ACCESS EXCLUSIVE: schedule the migration on a large production table. | before production |

## Phase 2 known limitations (accepted, documented)

| Item | When |
|---|---|
| Similarity is an exact cosine KNN, O(pages) per query (12 ms at about 1k pages). Add an HNSW partial index per (model, dimensions) when latency is measured as a problem. | at scale |
| `KEN_SIMILARITY_MIN_SCORE` is one global, model-specific value (0.78 for nomic). Bind it to the embedding identity, and re-calibrate per model with `evals/intelligence/benchmark.py`. | when the model changes |
| Embedding identity is the model name; re-pulling the same tag with new weights is not detected. Consider adding the Ollama digest. | before a model upgrade |
| The intelligence reader asks the provider for its identity on each request: fast normally, but a hung daemon adds the provider timeout. Add a short TTL cache and a request deadline. | 6 |
| `get_coverage` aggregates in Python, O(pages) per request (~300 ms at 1k pages). Aggregate in SQL or cache. | at scale |
| A job run whose finishing step itself fails stays `running` (no reaper). Losing the advisory-lock connection mid-run silently drops the lock. | 6 (scheduler) |
| Analysis has no per-page time budget (linear, about 1.6 s/MB of hostile text). | optional |

## Phase 1 known limitations (accepted, documented)

| Item | Where / when |
|---|---|
| A run killed with SIGKILL stays `running`; nothing reaps it. Its advisory lock is released, so it does not block the next run. | add a reaper or heartbeat column when runs become scheduled |
| If the DB connection holding the advisory lock drops mid-run, the lock is lost while the run continues | same |
| Alias documents (declared canonicals) are buffered in memory until the source ends: fine at the 500-page default, grows with very large runs | before crawls of tens of thousands of pages |
| A moved page (301 from its old URL) becomes a new page; histories are not merged across a move | if needed |
| Two pages declaring each other canonical (an alias cycle) store each other's content under swapped identities. This is deterministic and stable. | — |
| A CMS page deleted and recreated at the same URL with a new id is an `identity_conflict` until an operator resolves it | operator tooling (Phase 5) |
| `http` vs `https` and `www.` variants are never unified; a site serving both without redirects or canonicals shows duplicates | site configuration |
| The inventory reader uses the app's write-capable DB role. It is read-only by code, not by role or `SET TRANSACTION READ ONLY`. | Phase 5 RBAC / least-privilege role |
| httpx logs each fetched URL at INFO; a token in a crawled URL's query string would reach logs | lower httpx log level when real crawls start |
| A raw `href` containing credentials is stored verbatim (typed `invalid`, never fetched) — it is the page's own public markup | informational |
| No automated test enforces the inward dependency direction; the API → services edge is a by-name import | optional AST import test |
| **Desktop data-fetching decision** (ADR-0008) now covers the inventory screens too — they are server-rendered like `/system` | when Electron is scheduled |

## Carried forward from Phase 0 / 0.5

| Item | Target |
|---|---|
| Apply the identifier aliases (`PageId`, `DeploymentId`) to the remaining plain-`str` fields in `cms.py` (no wire change) | 3 |
| Freeze the autonomy decision enum (BLOCK / REQUIRE_APPROVAL / AUTO_APPROVE). TRD §16 `AWAITING_APPROVAL` is now `AgentRunStatus` (ADR-0012 §3). | 9 |
| **Version-aware event reads** (cleanup QA M1): `list_events` re-validates stored rows against today's rules | before 8 |
| Bump `asyncpg` 0.31.0 → 0.32.x after soak; record in ADR-0004 | when convenient |
| An ANN index; tuning channel weights on real labelled data | at scale / 8 |
| Starlette deprecation warning: `starlette.testclient` with `httpx` | when upgrading Starlette |
| CI workflow authored but never run (no git remote; pushing is ASK) | when a remote exists |
| Non-editable wheel build across the package roots (now five) not yet verified | before any distribution |
| QA L5: append-only `events` not enforced in the DB (unlike `page_versions`/`existing_links`, which now are); app connects as superuser `ken` | production integration |
| QA L8: floating image tags; CI actions pinned by tag; three identical compose images | before any shared env |
| QA L9: `/ready` stays public by design (probes; ADR-0013), 3 DB sessions per call | before any shared env |
| QA P1: the Phase 9 policy engine must read AUTO_APPROVE from a module-level `Final`, never a `Settings` instance | 9 |
| Re-verify the licence inventory of any packaged desktop bundle (`sharp` is LGPL) | before desktop distribution |
| 5432 guard does not resolve DNS (`127.1`, decimal IPs) | optional |
| Colima does not start at login: run `colima start` after a reboot | — |

**Closed in Phase 7:**
- PRD §7.12: `internal_link_impression` and `internal_link_click` with source, destination, link id and policy version (experiment id is derived, not stored — Phase 8 adds experiments).
- TRD §5 `link_events`; the GA4, Search Console and first-party providers behind one contract, with CSV fallback.
- `analytics.updated` is emitted, with a typed payload, in the same transaction as its import.
- Outcome attribution to plan, recommendation run, deployment, source page version, target, anchor and every policy/engine version.
- Dashboards for link and page performance, with before/after comparison and explicit confounders.
- The `verified → measured` transition, with an append-only measurement snapshot so Phase 8 and 9 can reproduce what was measured.

**Closed in Phase 6:**
- Deploy-time re-validation under lock: freshness, protections, live eligibility, existing links and density (Phase 3 QA gate).
- DB-level link-plan status integrity: a deferred constraint trigger with a transition record in the same transaction (Phase 3 QA M2).
- Inline `display:none` / `visibility:hidden` are detected as hidden, in mutation and in verification.
- `javascript:`, `data:` and other unsafe hrefs, and empty anchors, are refused (Phase 0 QA L6).
- QA L7, resolved as designed: a no-op rollback replay reports the original authoriser (it is the same operation), and the CMS audit log records each caller separately (`rollback_no_op` with the second actor). The deployment service also records the session actor on every rollback event.
- The CMS audit lands in `events`: `link.deployed`, `link.verified`, `link.failed` and `link.rolled_back`.
- Read-only GET sessions; the job reaper (`make jobs-reap`); the Phase 1/2 "stays running" gap.

**Closed in Phase 5:**
- Authenticated actors (Phase 3 QA H3 gate): actors come from the session.
- RBAC (TRD §23), with deny-by-default routes. 401 maps to `unauthenticated`, 403 to
  `permission_denied` and 405 to `method_not_allowed` (Phase 0 QA L1).
- Validation errors no longer echo input values.
- The `page_protections` trigger rejects DELETE and any UPDATE other than deactivation.
- The review inbox and editorial feedback capture (PRD §7.9).

**Closed in Phase 4:**
- mypy `strict` covers `ken_agents`.
- TRD §16 approval suspension: no process waits; plans persist as `awaiting_approval`.
- Agent evals with recorded fixtures (TRD §22): candidate selection, critic and no-action,
  plus protected-content and stale-page-version.

**Closed in Phase 3:**
- Phase 2 relationships to gone or stale targets: recommendation eligibility is computed live.
- QA L6 (part): no plan targets a protected page, and an open plan expires when its page becomes
  protected, gone or ineligible.
- Freshness diagnostics: an edited block whose anchor moved reports `context_changed` and
  `anchor_moved`.

**Closed in Phase 2:**
- The embedding model is pulled and used (`nomic-embed-text`, Apache-2.0), with the dimension
  read at runtime.
- Alembic `-x` parsing is strict: unknown or empty keys are refused. This follows a Phase 1
  incident in which a one-word `-x dburl=...` fell back to the dev DB.

**Closed in Phase 1:**
- QA L3: the mock provider's call log is bounded.
- QA L4: heartbeats are persisted only on the first tick and on status change (ADR-0005 amendment).
- QA L10: default `ken:ken` credentials are refused in staging/production, and `make_settings`
  ignores inherited `KEN_*` variables.
- Serialization-mode OpenAPI schemas: always-present fields are now required in TypeScript.

## Operating reminders

- Work one phase at a time. Under the current authorisation, continue to the next phase after
  each phase gate passes; stop only for ASK/BLOCK actions.
- Frozen contracts change only through the orchestrator (docs/CONTRACTS.md).
- Never add Qdrant, Neo4j, Redis, Kafka, Celery, Temporal or a paid API without an explicit user decision.
- Before crawling anything other than a local fixture, get the site approved. Before binding a test
  server, check that the port is free: 8765 on this machine is another local app.
