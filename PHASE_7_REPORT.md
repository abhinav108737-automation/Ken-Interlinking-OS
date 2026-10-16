# Phase 7 Report — Analytics

**Status: complete.** Executed under the user's 2026-10-14 authorisation for autonomous sequential
delivery of Phases 2–9. Each phase has internal gates, a local commit and a `phase-N-complete` tag.
Nothing has been pushed. **No Google API was called, no real site was instrumented, and no real
visitor data exists anywhere in this phase.** Every number in the local demo is either synthetic or
produced by a loopback collector, and is labelled as such.

**Design:** [docs/adr/ADR-0015-analytics.md](docs/adr/ADR-0015-analytics.md).
§12 holds the binding amendments from the pre-fan-out review and overrides §1–§11 wherever they
conflict; §12.8–§12.10 record the decisions settled while freezing and building; §12.11 holds the
QA outcomes.

## 1. What was built

| Capability | Implementation |
|---|---|
| **First-party capture** | A **separate loopback collector** (`GET /k.js`, `POST /e`, a health nonce), deliberately **not** part of the control API, so untrusted browser traffic never sits beside the auth surface. A ~120-line dependency-free beacon records impressions (`IntersectionObserver`, once per link per page view) and clicks (`sendBeacon`, a CORS-simple request needing no preflight). It opts out entirely on Global Privacy Control, Do Not Track, a consent flag or a staff marker, and sets no cookie. |
| **Pseudonymity** | A stored event is **only** (event type, plan, match mode, UTC date, minute-truncated receipt time, dedupe key, synthetic flag) — eight columns, enforced by a test against `information_schema`. No IP, user agent, page-view id, referrer, query string or cookie is ever persisted or logged; they exist only in memory during the request. The minute truncation is a database CHECK, so a caller cannot defeat it. |
| **Abuse resistance** | Validation runs in a fixed order and **every rejection returns 204 with identical headers and an empty body**, so the response is no validity oracle. The body cap is enforced on the bytes actually streamed (a lying `Content-Length` and chunked bodies both abort), with a read deadline. Origins must match exactly on scheme, host and port. Bots, prefetches and GPC are dropped. Rate limiting is keyed on the **ASGI peer only** — no forwarding header can choose a bucket — with a bounded LRU map and a global cap, and every request that reaches the limiter is charged. |
| **Providers** | `mock`, `csv`, `ga4`, `gsc` and `stored`, all behind the frozen `AnalyticsBatchProvider` via one factory. **Two canonical CSV formats only** (`ken_link_daily`, `ken_page_daily`), ISO dates only, exact headers, bad rows counted and never echoed, sha256 and basename recorded. |
| **Google egress (off by default)** | Refused **before any request** unless `KEN_ANALYTICS_GOOGLE_ENABLED=true`. Then: https only, the exact allowlisted hosts, `follow_redirects=False`, `trust_env=False` (so proxy variables are ignored), a read-only token re-read per import from a file that must be mode 0600, bounded 3-try backoff, and a fixed `google_auth_failed` on 401/403 with no retry. **Only the property id and the dates are ever sent** — never a URL, link id or page content. Enabling it is an ASK action. |
| **Imports** | One transaction. Replace semantics: refuse a synthetic/real or `via` mix (overridable only by an explicit, recorded `--replace-via`), delete the range, insert sorted with duplicate keys summed, write the immutable ledger row, run the measure sweep, emit `analytics.updated`. Re-importing the same input is byte-identical. End dates past each source's lag are refused, so partial days are never stored. |
| **Attribution** | The plan is the root. The **live window comes from `link_plan_transitions` only** (`deployed` → `rolled_back`), never from a deployment item stage — a CMS no-op records `already_applied` while the plan still becomes `deployed`. One query assembles plan, run, policy/engine/ranking/analysis/calibration versions, source and target plus their page versions, anchor and its origin, the agent run and verdict, the approving human action, the deployment, and the rollback. |
| **Before/after** | 28-day windows either side of the applied day, which is itself excluded as partial, with per-source day boundaries (GA4's property zone, Search Console's America/Los_Angeles, first-party UTC). Deltas and ratios appear only with 14+ covered days on both sides. Confounder flags are explicit, and a site-wide trend ratio is shown as context, never as an adjustment. **No causal claim is made anywhere.** |
| **Measurement** | `verified → measured` only, only by `system:analytics`, only on real data — **synthetic data never measures**. The sweep takes a non-blocking per-source deploy lock, skips busy and `rolling_back` sources (both checked inside the loop), and writes an append-only `link_measurements` snapshot in the same transaction, so what was measured stays reproducible after a later re-import. |
| **Dashboards** | `/analytics` (KPI tiles, range form, sparklines each backed by a table, top links, source freshness, recent imports), `/analytics/pages/[id]` (paired before/after bars, confounder chips), a performance panel with the full attribution chain on plan detail, a per-item table on deployment detail, and a link from inventory. Charts are hand-written SVG with table fallbacks — **no chart dependency**. Synthetic data carries a non-dismissible banner plus per-row badges. |
| **API** | Five **GET-only** routes needing `read`: summary, links, link detail, page impact and imports. Imports stay CLI-only, so no UI button can ever trigger external egress. Reads are bounded: ranking and paging happen in SQL, and page impact caps its comparisons. |
| **Demo data** | `make analytics-demo-seed` generates deterministic, repeatable synthetic metrics (seeded from a sha512, so independent of `PYTHONHASHSEED`), fed through the normal importer with `via=synthetic`. It refuses to run in staging or production and refuses to mix with real rows. |

## 2. Schema — migration `0008_analytics`

`analytics_imports` (insert-once, immutable ledger), `link_events` (the eight pseudonymous
columns), `link_metrics_daily`, `page_metrics_daily` and append-only `link_measurements`.

One shared guard: **UPDATE is always refused**, and **DELETE only inside a transaction that set
`ken.analytics_writer`** — verified not to leak across pooled connections. The ledger and
measurements are append-only even for the writer. Round trip head → 0007 → head is clean and
`alembic check` reports no drift; a downgrade leaves no analytics table or guard function behind.

**No experiment table or column**: experiments are deferred to Phase 8 and `NoExperiments` is
still wired in, so experiment attribution will be derived, never stamped onto events.

## 3. End-to-end smoke test (live, on disposable databases)

Databases `ken_smoke7` and `ken_cms_sim_smoke7`, one throwaway admin whose random password was
deleted afterwards, and every server on a free loopback port (never 8765).

**Setup:** 64 pages imported → 64 embeddings and 805 relationships → 48 plans → 64 simulated CMS
pages → 2 plans approved and deployed (`verified`).

**The real collector and beacon** (`make analytics-sim-serve`, the path QA could not reach):

| Step | Result |
|---|---|
| Combined server | simulated site and collector on two verified loopback ports, 2 live plans, `synthetic: true` (loopback origins) |
| The served page | the beacon tag is injected and both deployed links carry `data-ken-link-id` |
| `GET /k.js` | 200, `application/javascript`, `nosniff` |
| 3 real events posted (2 impressions, 1 click) | 204 → **3 rows stored** |
| Foreign origin, bot UA, GPC, replayed page view, unknown plan | **204 every time, 0 rows stored** |
| The stored row | 8 columns, no IP/UA/page-view id/path, receipt time `…06:06:00` (minute-truncated), `synthetic = true` |
| Aggregation, default window | `rows_read = 0` — correct: the default covers only **complete** UTC days, and the events were from today |
| Aggregation, explicit range | 3 events → **2 daily rows**, `plans_measured = 0` because synthetic data never measures |
| The five API routes | summary (2 live links, CTR 0.5, freshness per source and dataset), links, detail (live window, series, full attribution), imports (both ledger rows), page impact (4 comparisons) |
| Anonymous, and a 400+ day span | **401**, **422** |
| Web (production build) | `/analytics` 200 with the synthetic banner; plan detail shows Performance and the attribution chain; page impact shows "not a causal effect"; unknown page 404; anonymous → `/login`; CSP present; **the token appears in no page** |

## 4. Dependencies

**None added**, in Python or Node. Charts are hand-written SVG; HTTP uses the existing `httpx`;
Google auth deliberately uses a pre-obtained token rather than adding `google-auth` (and so
`cryptography`), which also keeps any long-lived private key out of the app. `pyproject.toml`
gained one `package-data` entry so the beacon travels in a built wheel.

## 5. Verification (final run, final code)

| Gate | Result |
|---|---|
| `make verify` | ruff clean, mypy clean (378 files), **4,398** Python tests passed, **1,230** web tests passed (43 files), web lint clean, compose ×3 OK, migrations at head, doctor **0 FAIL / 0 WARN** (21 checks) |
| Migration round trip | §2 |
| Live smoke test | §3 |

## 6. Reviews

**Pre-fan-out architecture review: APPROVED WITH CHANGES — 13 binding amendments, all applied
before any specialist started** (ADR-0015 §12). It cut roughly 30 % of the proposed scope:
experiments, landing-page tracking, search-query metrics, the purge command, JSONL import and a
standalone collector target all moved to Phase 8 or were dropped. It also caught three claims the
draft made about existing code that were wrong — most importantly that the live window must come
from plan transitions, not deployment item stages — and required the `link_measurements` snapshot
so Phase 8 and 9 can reproduce what was measured after a later re-import.

**QA/security review: PASS WITH REQUIRED FIXES**, no critical or high finding. It probed by
execution: 24 collector abuse classes (all 204, byte-identical), an attempt to force identifying
data into a log line, rate-limit steering via three forwarding headers, 200,000 rotating IPv6
addresses, Google egress with the flag off (zero transport calls) and lookalike hosts refused at
startup, proxy variables ignored, and the writer guard.

| Finding | Fix |
|---|---|
| **M1. An import whose every row was rejected deleted the range and exited 0** — a wrong or corrupt file silently destroyed a range of metrics. | **Fixed, not accepted.** An import that read rows but would write none is refused and rolled back; the stored rows survive. A genuinely empty source still clears a range deliberately. Three tests pin it. |
| **M2. `list_links` read fan-out was unbounded** (`O(live links × days)` in memory for one request). | **Fixed.** Aggregation, ranking and paging moved into SQL: at most one page of rows returns, constant in the range length. |
| L3. The rate limiter charged only valid requests, and the body read had no deadline. | Every request reaching the limiter is charged; the read has a timeout. Rejections remain 204. |
| L4. The `rolling_back` check was read before the per-source lock loop (TOCTOU), the one way measurement could race a rollback. | Moved inside the loop, after the lock succeeds. |
| L5. Nothing pinned the two behaviours the review amendments introduced. | Both added to the golden scenario: a CMS no-op whose only stage is `already_applied`, and a `deployed → failed` plan the collector accepts but which never measures. |
| I6, I7 | Accepted and recorded: timing still distinguishes a stored event from a rejected one, and an all-rejected import is audited as a failed job run. |

## 7. Incidents

- Two specialists were killed mid-task by transient API and network errors. Both were resumed
  from their own context, re-checked what was already on disk rather than redoing it, and
  finished. No work was lost or duplicated.
- One web test failed in the first gate run: a pre-existing focus assertion that raced a React
  effect. It was a test defect, not a product one. The fix waits for the effect instead of
  racing it; the assertion was **not** weakened, and deliberately breaking the component was
  used to confirm the test still fails when focus is missing.
- One genuine cross-workstream bug: the dashboard's empty state told operators to run
  `SOURCE=csv`, which the CLI rejects — `SOURCE` is the row's origin and passing `FILE` is what
  makes a run a CSV import. Corrected, and a test now pins every command the UI prints to a
  value the CLI accepts.
- Integration tests share one test database and truncate it, so concurrent specialist runs
  produced unrelated failures. Every reported failure was re-run serially before being believed.
- No real CMS, no Google API, no external network, no git push.

## 8. Known gaps

See NEXT_STEPS.md, "Phase 7 known limitations". The main ones:
- **First-party counts are spoofable** (anything a browser sends can be forged), so Phase 9 must
  never graduate a decision class on first-party data alone;
- outcomes are **correlational only** — no causal claim, and experiments arrive in Phase 8;
- no real-browser beacon test (Playwright still deferred);
- GA4 and Search Console are unproven against the live APIs by design;
- cross-source totals double-count the same event, and say so where shown.

## 9. Information still needed from you

- **Real analytics (ASK):** a GA4 property id and its time zone, a Search Console property, and a
  read-only OAuth token plus how it is refreshed. Everything stays refused until then.
- **GA4 setup, if you want link-level data:** register the event-scoped custom dimension
  `ken_link_id` and forward the beacon's two events. Data only accrues from registration onward.
- **Consent (ASK / legal):** the beacon sends behavioural events, which ePrivacy, GDPR and India's
  DPDP Act may treat as analytics requiring consent. Putting it on the real site needs your
  consent-manager decision; it reads `window.kenAnalyticsConsent`.
- **Where production first-party collection lives.** Until you decide, the aggregated daily CSV
  import is the contract and the collector stays local.
- Still open from earlier phases: create your accounts; the real CMS contract and credentials;
  a sample of real page HTML; browser verification; the self-describing-anchor rule; and whether a
  model critic's reject filters plans out of the queue.

## 10. Commit

`feat: complete Phase 7 Analytics`, tag `phase-7-complete` (local only).
