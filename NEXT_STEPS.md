# Next Steps

**Copy rule enforced, and seven drafts ready for the owner's decision (2026-11-20, later):**
the 12 live cluster sentences were rewritten to the owner's rule (two or more sentences, 32-38
words; deployments 55-66, 12 of 12 publicly verified) because all 12 had been the draft's internal
rationale verbatim. The rule now lives in `tools/v02-contextual-audit/copy-rules.mjs` (5 node
tests) and every applier checks it before any CMS request; the review dashboard shows it on each
card. **Awaiting owner approval, all dry-run valid against the live CMS and all passing the rule:**
`v02-relocated:1`, `:2`, `:4` (the three rolled-back links, moved to Market Trajectory under new
ids, because the old approvals covered other wording and another section), `cluster:1`, `:6`,
`:15`, and `topic-kuwait:7`. The six live `topic-kuwait` links run 25-28 words and predate the
rule; rewriting them is an owner decision. **Restart the dashboard** (`npm run dashboard:v02`) to
see the new statuses: a running server holds the code it started with.

**Finding: the public template hides much of each mapped report.** On three reports,
`map-rendered-sections.mjs` shows only Market Trajectory and Key Stakeholders render in full;
Growth Drivers shows 1 of 10 paragraphs. Visibility is per paragraph: seven live links sit in
Market Segmentation, Regional Analysis and Growth Drivers (corrected 2026-11-25; the first version
over-generalised).
See knowledge lesson 8. Worth raising with the site team.

**GitHub-inheritance sequence, honest state.** Step 1 (copy and evidence rule for all drafts) is
now done at the point that matters, the appliers. Steps 2-4 exist only as local prototypes and are
NOT integrated: sitemap/snapshot/diff is a standalone Node script over a hand-built 28-page file,
not `inventory.sources.public_web` with persisted snapshot ids (needs a migration); the graph view
is in the local review tool, not `apps/web`; RRF has tests but no caller in ranking. Each is its own
phase (the plan estimates 5-8, 4-6 and 4-7 days), and the next one should start only on instruction.

**Approved exact-market cluster batch LIVE on the test host (2026-11-20):** the 12 dashboard
approvals `cluster:2`, `:3`, `:4`, `:5`, `:7`, `:8`, `:9`, `:10`, `:11`, `:12`, `:13` and `:14`
are now CMS-verified and publicly rendered as deployments `43`-`54`. Twelve of twelve passed the
version-locked dry run, the apply, the CMS read-back and the rendered-DOM check with no rollbacks.
All are `exact_market_different_geography` siblings (Kuwait/Malaysia/Qatar) placed in
`v02_market_trajectory`, the one field the earlier batches proved the public template renders.
`cluster:1`, `:6` and `:15` carry no dashboard decision and were not touched. The audit record is
`outputs/contextual-v02-live/approved-cluster-batch-2026-11-20.json`; the applier is
`tools/v02-contextual-audit/apply-approved-cluster-batch.mjs`. Same limit as every batch before it:
the test host declares `noindex,nofollow`, so this proves copy, placement, rendering and rollback,
not authority transfer. **Note for review:** five of the twelve are reciprocal pairs (both
directions of guaiacol, organic dyes, ortho-phthalaldehyde, spine navigation and outdoor payment
terminals). Each direction carries its own comparative forecast fact rather than bare symmetry, but
the pilot's "no reciprocal inline links merely for symmetry" rule makes this an editorial judgement
worth revisiting with click data.

**Still open on the rolled-back placements:** `v02-new:1` (Kuwait electrosurgical devices) and
`v02-new:2` (Mexico cold chain) were re-approved at 07:55Z AFTER their rollback, and `v02-new:4`
(Kuwait female infertility) remains approved. All three failed `rendered_verified` because the
chosen CMS field is absent from the public template, so each needs a different, visibly rendered
section before it can be re-applied -- not a replay of the same placement.

**Approved Kuwait topic batch LIVE on the test host (2026-11-20):** dashboard approvals
`topic-kuwait:1` through `topic-kuwait:6` are now publicly rendered and CMS-verified. The first
canary uses deployments `35`–`37`; the approved follow-up batch uses deployments `38`–`42`.
`topic-kuwait:7` remains a shadow proposal because it has no approval. The batch audit is
`outputs/contextual-v02-live/approved-topic-batch-2026-11-20.json`. These links demonstrate copy,
placement, rollback and rendered-link safety only: the test host declares `noindex,nofollow`, so
they do not transfer search authority.

**GitHub inheritance sequence implemented as a governed MVP (2026-11-20):** the dashboard now
contains a filtered interactive Kuwait graph with existing, proposed and live edges; the graph
CLI builds snapshots, diffs two snapshots, finds shortest paths and discovers sitemap seeds; and
RRF fusion plus precision/recall tests is available in shadow code. Five RRF tests pass. The first
real judgment set contains 6 approvals, 6 rendered-live outcomes, 0 rejections and 0 measured
click outcomes. Therefore the reranker gate correctly remains closed: continue collecting real
negative labels and engagement events before training or promoting any learned ranker. See
`docs/handover/GITHUB_REPOSITORY_INHERITANCE_IMPLEMENTATION_2026-11-20.md`.

**Kuwait topic-cluster authority audit (2026-11-20):** seven new editorial drafts now appear in
the v0.2 dashboard with individual 91–97 contextual scores and a separate authority/indexability
gate. All seven are currently blocked for authority routing on `thekenconsulting.com`: the audited
source and destination pages declare `noindex,nofollow`, the draft `www` href differs from the
declared non-`www` canonical, and destination presentation remains pending. Keep editorial approval
separate from production eligibility. Next: map approved drafts to followable, indexable canonical
production URLs (if available), verify public destination evidence, then run CMS dry-run and
rendered-link verification. Search Console is required to label a page indexed/non-indexed or
ranking/non-ranking; internal linking alone is never recorded as a guarantee.

**Kuwait topic canary LIVE on the test host (2026-11-20):** owner-approved `topic-kuwait:1`
(Retail Inventory Forecasting → Order Management Systems) passed the atomic content-change dry run,
was applied as deployment `35`, and was verified in both CMS content and the public rendered DOM.
The link appears in Market Trajectory on `order management systems`. The durable audit record is
`outputs/contextual-v02-live/topic-kuwait-1-live-2026-11-20.json`. This remains a presentation
canary because the test host declares `noindex,nofollow`.

**Business Phase 2 direction (2026-11-19):** the approved engagement-oriented Priority
Interlinking plan is now durable project knowledge in
[`knowledge/BUSINESS_PHASE_2_PRIORITY_INTERLINKING.md`](knowledge/BUSINESS_PHASE_2_PRIORITY_INTERLINKING.md).
The Claude handover is
[`docs/handover/BUSINESS_PHASE_2_CLAUDE_HANDOVER.md`](docs/handover/BUSINESS_PHASE_2_CLAUDE_HANDOVER.md).
Start with the repository-backed gap analysis described there; build on items 29, 31, 32, and 34
rather than creating a parallel recommendation engine. This business phase is not the historical
repository software Phase 2.

**Gap analysis DONE 2026-11-19:** [`docs/handover/BUSINESS_PHASE_2_GAP_ANALYSIS.md`](docs/handover/BUSINESS_PHASE_2_GAP_ANALYSIS.md).
Measured baseline at that commit: `tests/unit` 4,657 passed, 32 skipped. It found that Business
Phase 2 needs **no second engine and conflicts with no ADR-0027 gate**, and that three of its four
headline gaps are policy data or owner decisions rather than missing code. It also found one
genuine structural gap: `link_events.plan_id` is a foreign key to `link_plans`, so the 3,213
persisted modules and 9,777 module items (item 31) **cannot be measured at all** — which is most
of Business Phase 2's intended surface area.

**Owner decisions, 2026-11-19** (recorded in full in the gap analysis §10):
D1 approve 2A-1 · D2 page-family routing classification for a *proposed* `recommendation.v2` ·
D3 a separate `priority` ranking component via ADR, never permanently through `commercial` ·
D4 rebuild the full eval corpus only AFTER the market graph (item 32); purpose-built fixtures for
2A-1 · D5 ADR for module-item analytics identity · D6 no auto-generated public hubs; curated
membership, shadow only · D7 `expert` is a taxonomy-v4 requirement, not invented now.

**Owner-set sequence:**

| # | Step | State |
|---|---|---|
| 1 | **2A-1** governed priority registry + shadow reporting | **DONE 2026-11-19, verified** (below) |
| 2 | Update the gap analysis and NEXT_STEPS with the verified result | **DONE** (this entry; gap analysis §11) |
| 3 | Draft the explicit-priority-component ADR (D3) | next — not started, by instruction |
| 4 | Draft the module-analytics-identity ADR (D5) | not started |
| 5 | Prepare the proposed `recommendation.v2` routing policy + tests, NOT activated (D2) | not started |
| 6 | Full eval-corpus rebuild — only after the market graph, item 32 (D4) | deferred; item 34 stays open |
| 7 | Public topic hubs (D6) and the `expert` taxonomy family (D7) | pending curated business data |

**2A-1 — what exists now.** Migration `0013_page_priorities` (head confirmed as
`0012_page_modules` before choosing it). `page_priorities`: 9 governed types, tier 1–5, reason,
owner, `effective_from`, optional `effective_to`, `retired_at`, created/updated by+at; page FK
`ON DELETE RESTRICT`.

**Validity is an interval, not a flag** (two lifecycle defects fixed 2026-11-19 before
acceptance). `ken_priority_validity` in migration 0013 returns
`[effective_from, min(effective_to, retired_at))`, clamped so it can never invert, mirrored in
Python by `PriorityDesignation.validity_end`, and a parametrised test holds the two
implementations to each other over seven cases.
* **Retirement records an INSTANT.** A boolean `active` answered only "is it live now", so a
  designation retired today reported as absent for every past date and a shadow report
  reconstructed for that date was wrong. `retired_at` truncates the interval instead, so the
  designation still resolves for dates on which it was genuinely in force. A future `--at`
  SCHEDULES the retirement, which is how an open-ended designation is handed over on a date
  without ever editing it. `retired_at` at or before `effective_from` yields an EMPTY interval:
  in force at no instant, blocking nothing.
* **The rule is non-overlap, not uniqueness.** `page_priorities_no_overlap` (trigger, with
  `pg_advisory_xact_lock` per page+type against concurrent inserts) refuses two designations of
  one (page, type) whose validity intervals overlap. The previous partial unique index
  `WHERE active` forbade a scheduled replacement and let an expired-but-still-active row block
  its own successor. Adjacent half-open intervals do not overlap, so `[Nov, Dec)` then
  `[Dec, …)` is accepted with no edit to the incumbent. **No `CREATE EXTENSION`:** a GiST
  exclusion constraint would need `btree_gist`, while `tstzrange` is core PostgreSQL — every
  migration in this chain states it creates no extension, and ADR-0017 removed the only one
  this project ever had.
* Timezone-naive instants are refused at the service boundary and in the dataclass, before any
  comparison (comparing naive with aware raises `TypeError` and would mask the real fault).

Two things the fix exposed, both now corrected:
* **A trigger's constraint name is not in its message.** `str(exc.orig)` therefore never matched
  `page_priorities_no_overlap`, so the CLI would have fallen through to a generic refusal. The
  name lives on the asyncpg error's `constraint_name`, which SQLAlchemy's adapter does NOT copy
  across — it keeps the original on `__cause__`. `violated_constraint()` unwraps both, and the
  schema tests assert through that SAME helper, so a regression in it fails the tests instead of
  silently degrading the message.
* **Adding a function or trigger breaks every "a downgrade removes exactly these" test.** 0013
  now creates two functions and a trigger, so `PRIORITY_FUNCTIONS` / `PRIORITY_TRIGGERS` join
  `PRIORITY_TABLES` in the conftest sets, and the head-state function inventory in
  `test_recommendations_schema` names both functions. Seven downgrade tests caught this. Vocabulary in `ken_core/priority.py` (deliberately not `ken_contracts`:
graduating it is the D3 ADR's decision). Domain + resolver `recommendations/priority.py`; shadow
`recommendations/priority_shadow.py`; DB read path + management `recommendations/priority_store.py`;
CLI `python -m ken_services.recommendations.priority_cli {add,retire,list,shadow}`.
**No frozen contract, policy file, live score, plan, module or analytics row is changed.**

**How priority is kept behind every gate (verified, not asserted):**
* The shadow reads only QUALIFIED candidates — survivors, plus those cut solely by
  `per_source_limit` / `diversity_limit`. Gate, eligibility, both floors (`below_min_relevance`,
  `below_min_score`) and the `link_density_exceeded` safety guard are excluded in SQL. The pipeline
  order this relies on (gates → min_relevance → placement → min_score → caps) was read from
  `pipeline.py:700–834`, not assumed.
* A designated target that is unpublished, gone, protected, noindex, non-canonical, HTTP-error,
  thin, type-excluded, unanalysed or stale earns nothing, with the refusing rule named — using the
  engine's own `page_eligibility`, under the shadowed run's own `analysis_version`.
* **Load-bearing tests:** unit — a tier-1 designation cannot admit a pair the REAL
  `geography_gate` / `market_gate` refuses, each with a positive control; integration — the real
  engine run with EVERY page designated tier 1 reports exactly the qualified set (8) and none of
  the 12 `market_unrelated` refusals; and every live table is identical before and after a shadow.

**Verified 2026-11-19** (after the two lifecycle fixes): unit + contract **5,269 passed**,
32 skipped (platform-only) — unit **4,768** (was 4,657) and contract 533. New tests: **69 unit**
(domain, shadow and CLI) and **58 integration** (priority schema + store, including the real
engine run). Migration round trip 9/9; all seven "a downgrade removes exactly these" tests pass
with `PRIORITY_TABLES` / `PRIORITY_FUNCTIONS` / `PRIORITY_TRIGGERS`; ORM↔DB parity on every
CHECK name and both indexes; SQL and Python validity intervals agree over seven cases; ruff +
format clean repo-wide; mypy clean except ONE pre-existing error
(`tests/unit/test_kenconsulting_cms_provider.py:154`, present at clean HEAD `8b5be34`).
Full integration suite: **1,293 passed, 4 failed, 5 skipped** (live-Ollama only) — the 4 failures are exactly item 34's known-red list by name, nothing else.

**CLI smoke on a real (test-DB) engine run:** 8 qualified candidates across 5 sources,
5 designated, 4 rank changes, 4 plan-set changes, **max priority share 0.140** at the
provisional 0.10 weight — first evidence for the D3 ADR's dominance limit.

**2A-1 known limitations:**
1. The shadow formula is an **additive approximation on the final score**
   (`min(1, live_score + value × 0.10)`); where a real `priority` component enters (inside
   relevance, before or after the placement factor) is the D3 ADR's decision. Every report states
   the formula and the assumed weight.
2. `SHADOW_PRIORITY_WEIGHT = 0.10` is provisional and is NOT in `ranking_weights.v1.yaml`.
3. No HTTP API or reviewer UI: both need contract models, which wait on the D3 ADR. The CLI is the
   local management surface.
4. Designation changes emit **no events**: `EventName` is a frozen vocabulary. Audit is the row
   history instead — there is no edit, only add and retire, so every decision leaves a row.
5. The plan-set comparison preserves each source's plan count but does not re-simulate the
   cross-source `diversity_limit`.
6. Eligibility is today's (live facts) while designations resolve at `--at`. Designations now
   reconstruct the past correctly, but eligibility does not: a report for a past instant pairs
   that instant's designations with today's eligibility. Historical eligibility would need the
   page facts as they were, which this slice does not store.
7. The integration corpus is India-only, so the geography gate is exercised at unit level
   (against the real gate) but not end to end.
8. The real-site corpus (`ken_real500`) has not been shadowed: that database is not migrated to
   0013, and migrating a database holding real-site data is left to the owner.

**Two findings outside 2A-1, flagged not fixed:**
* **Integration tests silently SKIP on this machine.** `tests/conftest.py` defaults to
  `localhost:5433`; Windows resolves `localhost` to IPv6 `::1` first, the container publishes only
  `127.0.0.1:5433`, and the 2-second connect timeout makes every integration test *skip* — so the
  suite reports green while testing nothing. Setting
  `KEN_TEST_DATABASE_URL=postgresql+asyncpg://ken:ken@127.0.0.1:5433/ken_interlink_test` fixes it
  (the Makefile already says `127.0.0.1`). Recommended one-line fix: change the default host.
* **Repo-wide lint had been red since 2026-11-17.** The archived live-test scripts committed then
  (`docs/live-tests/`) were linted; they are a verbatim record of what ran against the CMS, so
  they are now excluded in `pyproject.toml` rather than reformatted.

**Current state (2026-10-29):** Phases 7 (Analytics) and 8 (Learning) have passed their gates; see
[PHASE_8_REPORT.md](PHASE_8_REPORT.md) (full suite green after fixing a Windows-only test defect; ruff and
mypy clean). Phase 9 (autonomy, Qdrant, ADR-0017/0018) is verified in [PHASE_9_REPORT.md](PHASE_9_REPORT.md): 336 tests pass,
mode is `shadow`, nothing is published, and calibration is the missing piece. **Real-site bootstrap (ADR-0025):** a 498-page rendered export of kenresearch.com
is imported into the separate database `ken_real500`, analysed with the *proposed* `taxonomy.v2.yaml`, and has
126 link plans (10 sources reviewed by the local 8B critic). Earlier reports:
[PHASE_7_REPORT.md](PHASE_7_REPORT.md), [PHASE_6_REPORT.md](PHASE_6_REPORT.md), [PHASE_5_REPORT.md](PHASE_5_REPORT.md), [PHASE_4_REPORT.md](PHASE_4_REPORT.md), [PHASE_3_REPORT.md](PHASE_3_REPORT.md), [PHASE_2_REPORT.md](PHASE_2_REPORT.md), [PHASE_1_REPORT.md](PHASE_1_REPORT.md), [PHASE_0_REPORT.md](PHASE_0_REPORT.md).

**Execution mode:** the user has authorised autonomous, sequential execution of Phases 3–9, an
end-to-end local demo and a final report. Each phase has internal gates and a local commit plus
tag. Execution stops only for ASK/BLOCK actions.

**Next:** the owner reviews `taxonomy.v3.yaml` (item 26), the market-relationship gap (item 27) and the
first real review queue (under ADR-0018 people are the control plane, not per-link approvers).

**Full-site state (2026-10-31).** The whole 8,858-page inventory is imported, analysed under
`taxonomy.v3.yaml` and indexed in Qdrant (`ken_full`). ADR-0027 Stage A — geography gate, market gate,
market-keyword anchor — has run across it: **706,912 candidates → 853 plans**, 0 errors, 19 h 22 min,
with zero cross-region links (against 89 % before the ADR). Two anchor defects found by audit were then
fixed (fragment anchors inside a longer market name; the source's own market name as an anchor), and the
`/articles/` family re-run showed Title-Case fragment anchors falling 14 % → **0 %** and fully compliant
plans rising 34 % → **72 %**. `ken_full` is kept unchanged as the "before" record.

**Current corpus run (2026-11-09), database `ken_v3`, `tax3-alg2`** — the first run on the corrected
analyses, with the global-page geography fix and both anchor fixes in: **706,907 candidates → 915 plans**
and **25,221 opportunities**, 0 errors, 1 h 37 min (the earlier 19-hour figure was dominated by analysis
work this run did not repeat). The refusals say where the corpus stands: `market_unrelated` 245,149,
`geography_mismatch` 190,054, `page_type_incompatible` 166,535, `no_market_keyword_anchor` 99,604,
`no_natural_anchor` 2,425, `already_linked` 1,466. Three of those four large buckets are owner decisions,
not defects — `market_unrelated` is item 32 (the market graph), `page_type_incompatible` is item 29 (which
page families may link), and `no_market_keyword_anchor` is the structural limit of inline anchors that
item 31's modules exist to answer. `make modules` then wrote **3,213 modules / 9,777 items** into the same
database in 6 seconds; both are visible in the console at `/recommendations` and `/recommendations/modules`.

**Running a corpus job:** set `KEN_DATABASE_URL`, `KEN_TAXONOMY_PATH` (v3) and `KEN_QDRANT_COLLECTION`
together. They must agree — see item 28 for what happens when they do not.
**Windows tip:** set `KEN_TEST_DATABASE_URL` with `127.0.0.1`, or the integration tests skip silently.

## Needed from you before real production ingestion

The inventory works end to end on fixtures, exports and a mock CMS. Ingesting real Ken Research
content needs **one** of the three sources below, plus a few decisions. No production URL appears in
any governing document, so none was guessed.

| # | What | Why | Permission level |
|---|---|---|---|
| 1 | **Authoritative source: DECIDED 2026-10-29 (ADR-0025).** A CMS export for now (the Playwright-rendered export of `www.kenresearch.com`); a CMS read API is also authoritative and will follow. | One authoritative source per site avoids version churn (ADR-0009) | decided |
| 2a | *Public site crawl: APPROVED 2026-10-29* for `https://www.kenresearch.com/` (read-only, ≤ 500 pages). Done once, as a stratified sample of 498 of about 9,000 pages. A **full** inventory needs a fresh approval for the larger cap. | The crawler has no default site | done (sample); ASK for more |
| 2b | Politeness used: 1 s between requests, one request at a time, `robots.txt` honoured. Crawl window and a contact URL for the user-agent are still open. | Load on the production site | decision |
| 3a | *If a CMS read API:* its list and get endpoints, pagination, page-id stability, how drafts and publication state are exposed, and the field that holds the page template/type | `CMSContentSource` is complete; only a real `CMSInventoryReader` is missing | — |
| 3b | *If a CMS read API:* **read-only, least-privilege** credentials (TRD §23) and where they are stored | Never in source control | **ASK** |
| 4 | *If an export:* a sample export, so it can be mapped to the documented JSONL format (`docs/CONTRACTS.md`) or given its own adapter | Offline bootstrap with no load on the live site | — |
| 5 | **Scope:** which sections are in scope (e.g. only `/industry-reports/…`), which are excluded, and which pages are **protected** (never to be modified later) | Phase 3 eligibility and Phase 6 safety depend on it | decision |
| 6 | Is the site multilingual? | Language comes only from markup and headers (no detection library) | decision |
| 7 | **Ken's real taxonomy.** `policies/taxonomy/taxonomy.v3.yaml` (**proposed**, derived from every sitemap slug, ADR-0025; it supersedes the sample-derived v2, see item 26) needs your review: 13 industries, 5,451 markets, 58 geographies, real page types. Still missing: a **company/product gazetteer** (entities are empty), market tags for articles/surveys/case studies (5–23 % today vs 72 % for reports), and protected-page rules beyond legal pages. Promote it by setting `KEN_TAXONOMY_PATH` and re-analysing. | Classification quality on the real site | decision + data |
| 9 | **Self-describing anchors: DECIDED 2026-10-30/31 (ADR-0027).** The anchor is the target's own market keyword plus `market`, derived from the target's identity, never from the broad `market_id`. Cross-*region* pairs are refused outright by the §1 geography gate. For **containment** (country → a region containing it, e.g. a UAE article → a GCC report) the owner decided on 2026-10-31 that a **bare** keyword anchor is acceptable, because the target is a superset of the reader's geography; measured on 16 of 57 article plans. The conditional rows — different countries in one region, and global source → one country — still require the geography inside the anchor. | Editorial policy | decided |
| 11 | **Create your own accounts.** No user exists after install, and none was created for you: run `make user-create USERNAME=… ROLE=operator_admin`, then reviewers/viewers as needed. | The UI and API require a session from Phase 5 on | action (you) |
| 10 | **Does a model critic's `reject` filter a plan out of the human queue, or only annotate it?** Today it filters: the plan stays `proposed`, with its reasons visible. The local qwen3 critic kept 23 % of plans out of the queue on the demo, including 1 of 20 GOOD plans (evals/agents/README.md). | Editorial trust in a local model; a one-line policy/graph change either way | decision (orchestrator default: filter) |
| 12 | **Real CMS integration (ASK). THE API ARRIVED 2026-11-12.** The technology team delivered a working internal API at `https://www.thekenconsulting.com/app/product/internal` with a read key and a write key: read, validate, apply, rollback, a `protected` flag, an opaque `version`, a 409 on version mismatch, plus a page-list endpoint and a `related` block we had not asked for. It is a near neighbour of the handover contract, not that contract, so **`KenConsultingCMSProvider` (ADR-0028) translates the dialect at the adapter boundary** — trailing slashes, two keys, one link per apply call with a composite `deployment_id` and a compensating rollback, 1-based field-scoped `occurrence`, site-relative `href`, `protected` read from the report. 29 unit tests; the frozen contract, the plans, the deployment service and the review flow are untouched. Writes stay disabled and the write key is not read while they are. **Still blocking the first write:** (a) phase 2 — which of a report's 12 fields a plan's anchor lives in (`link_plans.preview` already holds the context, so this needs a deploy-time resolver, not a contract change); (b) the eight open questions in ADR-0028, of which **idempotent replay of an `operation_id` is the hard one** — constraint 12 requires every mutation to be idempotent, and until it is answered the adapter never retries a write; (c) the read key and your approval to call their API at all; (d) confirmation that `thekenconsulting.com` carries the same slugs as the `kenresearch.com` URLs all 915 plans address. | Phase 6 ran only against the mock and the simulated CMS | **ASK** |
| 13 | **Browser verification (amends TRD §2 / PRD §7.11).** **Decided 2026-10-29 (ADR-0024):** Playwright is adopted as **development tooling only** (MCP server for screenshots and page inspection). Deployment verification still parses the served HTML deterministically (ADR-0014 §6); a browser inside the product remains undecided. | Real pages stream content in hidden segments, so rendered inspection finds what a parser misses | decided (tooling); product use open |
| 14 | **Real-template parity.** Run `make cms-sim-seed --export` on a sample of Ken's real HTML, or give us 10–20 real page bodies, to confirm that block hashes match between the inventory and the CMS body. Where parity breaks, the mutator refuses; it never guesses. | It proves deployment works on real templates | data |
| 15 | **Real analytics: ENABLED and PROVEN LIVE 2026-10-29.** GA4 (property 307716462) and Search Console were read with the service-account key (ADR-0022); a one-week page import read about 35,000 rows from each into `ken_real500`. Most rows are unresolved until the full inventory exists (they name pages outside the 498-page sample). The Search Console **query** import (item 22) is still a separate approval. | GA4/GSC calls are external egress | enabled; verified |
| 16 | **GA4 setup, only if you want link-level GA4 data.** Register the event-scoped custom dimension `ken_link_id` and forward the beacon's two events (`internal_link_impression`, `internal_link_click`). | GA4 only accrues custom-dimension data from registration onward, and high cardinality can produce `(other)` rows | action (you) |
| 17 | **Beacon consent: approved in principle 2026-10-29.** No deployment was made (CMS writes are mock-only). The legal question (ePrivacy, GDPR, India's DPDP Act) is not settled by an approval; whoever deploys it must choose the consent-manager route. It reads `window.kenAnalyticsConsent` and honours GPC and DNT. | Serving it from the real site | approved; not deployed |
| 18 | **Where production first-party collection lives.** Until you decide, the aggregated daily CSV (`ken_link_daily`) is the import contract and the collector stays loopback-only. | The local collector is a demo and a reference implementation, not a production service | decision |
| 8 | **PRD v1.1 §17.1 wording:** "a genuine native degraded mode" now holds only for schema ≤ 0002 (pgvector is required from Phase 2, ADR-0010) | A product-document revision, for your approval | decision |

| 19 | **Hot policy reload (Phase 8, ADR-0016 §16).** Promotion writes a new policy file and changes the ACTIVE policy path, and `Settings` is frozen, so a promotion needs an operator restart. Hot reload would be a material architecture change. It is also moot while promotion is blocked. | Operability of promotion once it is unblocked | decision (**ASK** — architecture change) |
| 20 | **Turning off learning demonstration mode (Phase 8, ADR-0016 §16.1).** `KEN_LEARNING_DEMONSTRATION_MODE` defaults `true` and refuses every promotion, so Phase 8 exits with zero promotions by construction. The corpus is ~65 **synthetic** pages, 48 plans and scripted review actions: the one stream the design calls trustworthy — human editorial decisions — is the stream with no real data. Real editorial volume is the gate on learning being more than plumbing. | Nothing on synthetic data should be promoted | decision (**ASK**) |
| 21 | **Enabling holdout experiments (Phase 8).** `KEN_EXPERIMENTS_ALLOW_HOLDOUT` defaults `false`. A holdout deliberately WITHHOLDS inbound links from its control **target** pages, a real editorial and SEO cost on the live site. The share is capped at 20 % by a database CHECK (policy default 10 %), and while the setting is off the lifecycle service refuses activation AND withholds the transaction-local `ken.learning_experiment_activator` setting migration 0009's guard requires — so no code path, not even a raw UPDATE, can start one. `policy_ab` (both arms get links) is unaffected. | Withholding links from real content is your decision | decision (**ASK** to enable) |
| 22 | **The deferred Search Console query import** (`page_query_metrics_daily`, `--queries`), without which search-side learning has no data at all. It is Google egress. | Search signals stay diagnostic-only and currently empty | **ASK** |
| 23 | **`review_performance` (ADR-0012 §42) is carried to Phase 9**, not implemented in Phase 8. The reviewer-performance view it reserves for "Phases 6–8" needs editorial volume that the synthetic corpus does not have; it is recorded here rather than left silent. | Avoids a silent gap against ADR-0012 | decision (orchestrator default: defer) |
| 24 | **The sitemaps cover only about 18 % of the live site (ADR-0025 addendum).** Search Console and GA4 report 65,602 distinct site paths with traffic in about 12 weeks; only 7,984 are in the 8,858-page inventory. A random sample of 80 of the other 57,618 found 70 % live (HTTP 200), 15 % redirects and 15 % 404 — roughly 40,000 live pages are missing. Decide whether to crawl them (about 8 hours at the approved politeness, about 5 GB) and from which seed list (traffic URLs, listing pages, or both). | The inventory defines which pages can be linked at all | decision (**ASK** — a large crawl) |
| 25 | **Similarity at scale: RESOLVED 2026-10-30 (ADR-0026).** On your decision (Qdrant, not `numpy`, to match the engineering stack), the Phase 2 relationship step now searches through the vector store when `KEN_VECTOR_STORE=qdrant`. Measured: recall@10 = 1.000 against the exact scan on 497 and 8,858 pages; 17 ms per page; the full 8,858-page run took 16 minutes, with 88,282 semantic edges. Default stays `memory`. Use one `KEN_QDRANT_COLLECTION` per corpus. **Correction:** the claim that "the recommender already used the store" was wrong — it took the exact in-process fallback and rebuilt an index per source (~5.6 h projected); the store is now threaded through `run_recommendations`. **Full-scale recommendation run measured 2026-10-31:** 8,858 sources, 706,912 candidates, 853 plans, 0 errors, 19 h 22 min. | Unblocks full-site relationships | done |
| 26 | **Taxonomy v3** (`policies/taxonomy/taxonomy.v3.yaml`, proposed): 5,451 markets and 58 geographies derived from every sitemap slug; 35 % of the markets sit in a catch-all industry. v2 (derived from the 498-page sample) is kept as the record, since a taxonomy is never edited in place. v3 tags 96 % of the sample against 38 % for v2, but it was derived from the same site, so that measures coverage, not correctness. Still needs your review and a company/product list. | Replaces the v2 proposal in item 7 | decision |
| 27 | **Taxonomy v3 declares no market relationships, and that is now load-bearing.** 0 of 5,451 markets carry an `adjacent` / `complementary` / `upstream` / `downstream` edge — the generator never emitted them. So ADR-0027 §2's market gate enforces "same market or taxonomy parent/child" only, its "declared supply-chain or complementary relation" clause is unreachable, and `market_unrelated` refused **240,383** candidate pairs on the full corpus. The stopgap shipped 2026-10-31: a `related_market` sibling pair qualifies when the embeddings independently agree (semantic similarity ≥ `KEN_SIMILARITY_MIN_SCORE`, 0.78), failing closed when either signal is missing. Declaring real edges is the proper fix and needs your domain input — notably whether a stated supply-chain dependency (India dry fruits → Africa cashew, which you called *acceptable*) should also open the cross-region door that §1 currently refuses. | Decides how much legitimate cross-market linking is reachable at all | decision + data |
| 28 | **FIXED 2026-11-09** (`pipeline.analysis_version_absent`). A run whose corpus holds analyses but **not one** at the run's own `analysis_version` now fails with the fixed reason `no page in the corpus carries this run's analysis_version`, in seconds, instead of succeeding after hours with nothing written. The condition is narrow on purpose: a corpus that was never analysed still proceeds (a fresh install, or the inventory running ahead of analysis), and so does a run where some pages match and others are ineligible for their own reasons. Six tests cover the three states, including the regression that caught the first draft — `PageFacts.analysis` only *prefers* the run's version and falls back to the newest of any version, so testing it for `None` would have let a wholly stale corpus through. Original finding below. | A silent no-op is the worst failure mode for a long batch | done |
| | *Original finding, 2026-10-31:* **A wrong `KEN_TAXONOMY_PATH` makes a run report success with zero output.** Eligibility requires `analysis_version` to match (`tax<N>-alg1`, from the taxonomy version). Point the engine at v1 while the corpus was analysed under v3 and every page is refused as `stale_analysis`: the job still exits `succeeded`, and the only signal is the `rejected_source_ineligible` counter. This cost one wasted run on 2026-10-31. | — | superseded by the row above |
| 31 | **A related-reports module is the missing capability, and it is now the blocker on the owner's Priority 1.** Decided 2026-11-01 (ADR-0027 §1b/§1c): same-market cross-region links are allowed, and 962 market cores span more than one geography (3,186 pages; 372 report cores carry 3+ geographies — `telecom` 14, `car rental` 14, `coffee` 13). But with the gate relaxed, **104 of 132 sibling pairs (79%) still die at `no_market_keyword_anchor`**: §4 obliges the anchor to name the target's geography and §3 allows only existing text, and a Malaysia car-rental report never writes "Indonesia car rental market". Inline anchors are structurally the wrong surface. A related-reports module, where the link text is the target's own title, names the geography by construction and needs no prose rewriting. (The "KSA Modular Kitchen Furniture Market" defect was an inline anchor placed *inside* such a module — the module is right, the inline anchor in it was wrong. Inline anchoring in those blocks was stopped on 2026-11-01 by `placement.block_is_prose`, verified 17 plans → 0 on a 14-source probe, so the surface is now free for the module.) **The proposal side is built** (`ken_services/recommendations/modules.py`, 15 unit tests, 2026-11-01): deterministic regional-sibling modules, ranked in tiers — a region containing the reader's geography, then global, then a neighbour in the same region, then elsewhere — with orphan and under-linked targets first within a tier, and reason codes on every item. **Corpus-wide result, measured on the persisted tables after the full `tax3-alg2` run of 2026-11-09: 3,213 modules of 8,858 analysed pages, 9,777 items, 778 modules full at six** (sizes: 1,124 modules of one item, 525 of two, 382 of three, 231 of four, 173 of five, 778 of six) — against **915** inline plans for the whole site, so roughly 10× the linking with no sentence rewriting. It proposes only; nothing is written and no CMS contract is assumed. Tiering was verified on real output, not only in tests: an Egypt source ranked its Middle East neighbours first (Bahrain, Oman, Qatar, Saudi Arabia, UAE) where it had previously produced plain alphabetical order, and a Saudi source ranked GCC — the region containing it — first. That alphabetical order had been a real bug: `global` is an ancestor of every geography, so an ancestry test that keeps it makes every pair "share a region" and collapses all candidates into one tier. **PERSISTED 2026-11-09** (migration 0012, `page_modules` / `page_module_items`): the recommendation job now builds modules in the same run as the plans, from the world already in memory — no model call, no extra query — and `GET /recommendations/modules` serves them read-only with items in render order. The table carries **no status and no transition history**, unlike `link_plans`: a plan has those because a reviewer approves one and the deployment service applies it, whereas a module has nowhere to be applied until item 12 is decided, so a status would record a decision that cannot be carried out. Modules are derived state, replaced per run like `opportunities`, and items cascade from their module. `make modules` rebuilds them alone in seconds, under the same job kind (so the advisory lock still serialises it against a plan run), because a full plan run on the corpus costs about four hours of retrieval, scoring and placement that a module needs none of. The reviewer screen is live at `/recommendations/modules` (2026-11-09), one row per page with items in render order, the target's geography label and the reason codes on each item. Still needed: the CMS surface decision (item 12) before a module can render on a page. | Unlocks Priority 1 and Priority 2 without Stage B's editorial risk | proposals, persistence, API and reviewer screen done; CMS surface open |
| 33 | **FIXED and verified 2026-11-02** (`ANALYSIS_ALGORITHM_VERSION` 1 → 2). A page that declares itself global in its title or URL slug now carries the global node as its top geography mention. Measured before/after on the same 8,858 pages: **mislabelled global pages 1,103 → 0** (612 had been Asia Pacific, 239 North America, 96 China). Checking the fix found the mirror defect: a bare "does it say global" test moved 11 pages that also name a specific place, about eight of them wrongly ("India Coffee Exports … vs Global Peers" became global). Resolved by Ken's own identity convention — the FIRST geography named wins, slug before title — which took the conflicts to 3, all correctly global ("/global-nail-salon-growth-opportunity-india"). Nine unit tests cover both directions. **Consequence:** every analysis stored under `tax*-alg1` is stale, so the corpus needs re-analysing wherever it is used; `ken_full` still holds alg1. Original finding below. | A whole row of the geography table was dead code | done |
| | *Original finding, 2026-11-01:* `/industry-reports/global-oat-milk-market` has primary geography **Asia Pacific**; `/industry-reports/global-trade-management-market` has **North America**. Cause: `gates.primary_geography` takes the highest-scored geography *mention*, and on a global page the most-discussed country or region outscores the word "global". Consequences seen in real output: a global page links to a regional report on its own market name ("The Global **Methanol Market**" → Asia Pacific report, 6% of the 1,028-plan run), and a related-report module lists the global report under a country label. Fix belongs in analysis, not in the gate: a page whose slug or title declares `global`/`worldwide` should carry the global node as primary regardless of mention counts. Needs a re-analysis of the corpus after the change. | — | superseded by the row above |
| 32 | **Priority 2, adjacency, needs the market graph the taxonomy lacks.** The owner's model (2026-11-01): a reader of an EV report wants charging-station and battery markets, because those are components of the same decision; then segments (electric bus, electric car); ranked by which genuinely helps. This is item 27's missing relationship edges, with a purpose. Proposed shape: the local model proposes **typed edges between the 6,592 market cores once, offline** (`component_of`, `complement_of`, `upstream`, `downstream`, `substitute_of`, each with a rationale and provenance), Pydantic-validated, scored against the owner's labels, shadowed, then explicitly promoted — never per-candidate inference at run time, which would be millions of calls and unexplainable. Serving stays a deterministic graph lookup. Ranking combines edge type, embedding similarity (Qdrant, already there) and **co-demand from the live GA4/GSC data** — the pages real readers visit together — which is the engagement signal the owner is optimising for. | The only part of the owner's model that genuinely needs a model | design + **ASK** before adding an agent node |
| 30 | **Eval scorers that re-implement a decision drift silently.** Found 2026-10-31: the `anchor-selection` scorer drove the pre-ADR-0027 `placement.anchor_phrases` while the pipeline had moved to `market_keyword_anchors`, so the suite had stopped gating real anchor behaviour and stayed green the whole time. Fixed, and the dataset reissued as `core.v2.jsonl` (dataset_version 2). **`shadow.py` FIXED 2026-11-09:** its restatement of the engine now calls the production geography gate, market gate and `market_keyword_anchors`, with the pipeline's own `enclosing_phrases` (market phrases, the source's own market name, every corpus title). It had been running the pre-ADR-0027 anchor path with no gates at all, so every pair it proposed and production refused was a false diff. A restatement is a liability by construction: the durable fix is one callable both paths use, and that refactor is NOT done. The audit itself turned up item 34, where the drift runs the other way. | A gate that cannot fail is not a gate | fix (ours) |
| 34 | **The agent eval suites now assert nothing, and they fail loudly saying so (found 2026-11-09).** `tests/integration/test_agent_evals.py::test_recorded_agent_cases` reports FIXTURE DRIFT on 22 of 48 cases with the same shape: *engine proposes [], fixture records [...]*. The cause is not a regression but the ADR-0027 gates meeting a corpus built for the engine that preceded them. Of 78 fixture plans, **46 cross a geography** (an India cold-chain source linking to Qatar logistics and Saudi solar) and most of the rest are **adjacency** links within one geography (India EV -> India lithium-ion battery *manufacturing*), which `market_unrelated` refuses while the taxonomy declares no relationships (item 27). So the critic, destination-selection, no-action and malformed-output suites have no plans to score at all. **The fixtures were deliberately NOT re-recorded**: re-recording would write empty expectations and leave four suites permanently green while measuring nothing, which is the same defect as item 30 with the opposite sign. **It is wider than the agent suites.** `test_recommendation_evals.py` fails the same way on the 65-page demo corpus itself — *"the engine proposed nothing on the demo corpus"* — so `test_demo_corpus_recommendations_pass_every_gate` and the placement-safety test are red too. **The whole synthetic corpus is inert under ADR-0027.** Rebuilding it is a coupled change, which is why it is not done here: `research_site.jsonl` (65 pages), `recommendation_judgments.json` (1,831 lines of labels), `relatedness.json`, `analysis_expected.json` and 48 agent fixtures all key off the same pairs. Item 32's market graph would change what is permissible again, so doing it now means doing it twice. **Red tests with a known cause, exactly four, measured on a full integration run (1,232 passed): `test_agent_evals::test_recorded_agent_cases`, `test_agent_evals::test_model_disabled_equals_the_deterministic_rule`, `test_recommendation_evals::test_demo_corpus_recommendations_pass_every_gate`, `test_recommendation_evals::test_placement_safety_page_never_anchors_under_excluded_markup`.** **UPDATED 2026-11-19.** Two of the four are resolved and the cause of a third is now understood. (a) The placement-safety test is GREEN: its fixture was not merely inert, it was *vacuous* -- its body still carried the free editorial phrases of the pre-ADR-0027 anchor path, so no anchor could be placed anywhere on the page and the test passed without exercising a single exclusion. Repaired with phrases `market_keyword_anchors` actually offers, and mutation-checked (narrowing the allowed set makes it fail). (b) `test_demo_corpus_recommendations_pass_every_gate` no longer proposes nothing: under [ADR-0029](docs/adr/ADR-0029-context-anchors-when-the-market-keyword-is-absent.md) context anchors it produces plans on 7 sources with `precision_at_3_macro` **1.00**, no bad examples and no anchor mismatches. It is still red, but now on `recall_good_macro` 0.1833 against a 0.70 floor calibrated for the pre-ADR-0027 engine -- a floor question for the corpus rebuild (D4), not an inert corpus. The agent suites are untouched and still need the rebuild. The fix is a corpus whose links the current rules actually permit (same market, regional siblings, taxonomy parent/child) — or item 32 first, then one rebuild. | The synthetic eval corpus predates the gates; four agent suites and two engine gates are decorative | fix (ours) — needs an eval corpus decision |
| 29 | **Page-family routing is declared for three families and the site has twelve (measured 2026-11-09).** `recommendation.v1.yaml`'s `page_type_targets` names only `report`, `insight` and `other` as sources, and only `report`, `insight` and `hub` as targets. The corpus under `tax3-alg2` holds **report 7,085, survey 906, benchmark 496, insight 208, case_study 55, pov 53, other 32, brand_comparison 9, consulting 7, corporate 4, transactional 2, hub 1**. So nine of the twelve families have no source row at all and can never propose a link, and nine can never receive one — `benchmark` (496 pages, a real Ken product family) is invisible in both directions, and `hub` can be a target but never a source. The policy predates the real taxonomy; this is 166,576 `page_type_incompatible` refusals, most of them unexamined rather than intended. Insight pages are 2% of the corpus yet yield plans at 0.27 per page against 0.10 overall, and supplied almost every anchor that survived editorial review, so the families are not interchangeable and the question is worth answering properly. **Needs your decision, not ours:** which families may be sources, which may be targets. The mechanism already exists — it is policy data — but a change means `recommendation.v2.yaml`, never an in-place edit, and every past plan records the version that produced it. | Routing now matters more to quality than scoring does | decision (evidence ready) |
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

### Kuwait Cloud-Based topic review — 20 November 2026

- Reviewed all 28 reports in current CMS and public HTML. Seven editorial groups cover 24 reports;
  four specialist topics remain separate. These are proposed research groups, not taxonomy changes.
- Confirmed three existing visible links and prepared seven additional bridge drafts in the local
  dashboard's `Kuwait topic clusters` filter. No CMS changes were performed.
- Next: review the drafts, check destination presentation where the exact CMS passage is absent
  from public HTML, then run version-locked CMS validation for approved proposals.
- Detailed evidence: `docs/evaluation/V02_KUWAIT_TOPIC_CLUSTER_RESULTS_2026-11-20.md` and
  `outputs/contextual-v02-shadow/kuwait_topic_cluster_results.md`.

- Work one phase at a time. Under the current authorisation, continue to the next phase after
  each phase gate passes; stop only for ASK/BLOCK actions.
- Frozen contracts change only through the orchestrator (docs/CONTRACTS.md).
- Never add Qdrant, Neo4j, Redis, Kafka, Celery, Temporal or a paid API without an explicit user decision.
- Before crawling anything other than a local fixture, get the site approved. Before binding a test
  server, check that the port is free: 8765 on this machine is another local app.
