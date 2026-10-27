# ADR-0015 — Phase 7 Analytics: first-party capture, GA4/GSC/CSV import, attribution, dashboards

- **Status:** Accepted, 2026-10-15. **§12 holds the binding amendments from the pre-fan-out architecture review, and §12 overrides §1–§11 wherever they conflict.**
- **Related:** PRD §§5 G3/G5, 7.5, 7.12, 8; TRD §§5, 11, 15, 20, 22, 23; ADR-0011 (plan id is the link id), ADR-0013 (RBAC), ADR-0014 (deployment, `data-ken-link-id`, §8 "attribution must not depend on the attribute")

## Context (what the code already gives us)

- `JobKind.ANALYTICS_IMPORT` and `EventName.ANALYTICS_UPDATED` already exist, so this phase adds no new event name and no new job kind.
- `LinkPlanStatus.MEASURED` ("outcome metrics are attached") is allowed from `verified`, and only a `system:` actor may set it. `deployment/service.py` `LINK_ON_PAGE` already counts `measured` as on-page, so rollback from `measured` works today.
- `ExperimentGuard` (recommendations.py) and `filters.NoExperiments` are documented as "filled in Phase 7".
- The frozen `AnalyticsProvider` is read-only and returns `LinkMetrics` / `PageMetrics` per `DateRange`. `MetricSource` has `ga4 | search_console | first_party | csv`.
- `link_plans` content is immutable and carries `engine_version`, `policy_version`, `ranking_policy_version`, `analysis_version`, `calibration_version`, `run_id`, `supersedes`, `anchor_source`, `reason_codes` and `source_page_version`. `agent_run_plans` (with `agent_runs.graph_version`) and `review_actions` hold the agent and human decisions. `deployment_items.change_id = plan_id` is enforced by a CHECK.
- The simulated site preserves `data-*` (`preserves_data_attributes = True`). A real CMS may strip it (`http.py`: `None`).
- Inventory normalisation and the deployment source map both drop `<script>`, so an injected script cannot change content hashes. This still needs a parity test.

## Decision

### 1. First-party capture: a separate loopback collector plus a beacon. Chosen.

**Options considered:**
- **(a) A public `/collect` route on the control API.** Rejected:
  - it breaks the ADR-0013 invariant that the public routes are exactly `/health`, `/ready` and `POST /auth/login`;
  - it puts untrusted browser traffic next to the auth surface;
  - in production the control API must never face the internet, while a collector must.
- **(b) Synthetic data plus file import only.** Rejected: nothing would be captured from real browsing, end to end.
- **(c) Adopted.** A collector ASGI app with no framework, built like `sim_site.py`, in `ken_services/analytics/collector/`:
  - **Routes:**
    - `GET /k.js`: the beacon, `application/javascript`, `nosniff`, `cache-control: max-age=300`;
    - `POST /e`: events;
    - `GET /__ken_collector/health`: a per-instance nonce;
    - everything else is a 404, or a 405 for a wrong method.
  - **Binding:** loopback IP literal, port 0, never 8765, with nonce-verified listener startup, all reused from sim_site.
  - **Gating:** a non-loopback bind needs `KEN_COLLECTOR_ALLOW_REMOTE=true`, which is ASK.
  - **Process:** `make analytics-sim-serve` starts the simulated site and the collector in one process, on two verified ports. `make analytics-collect-serve` starts the collector alone.

**sim_site change (additive):** `render_page(page, *, beacon_src: str | None = None)` and `SimulatedSite(..., beacon_src=None)`. When set, `<script src="{collector}/k.js" defer data-ken-collector="{collector}/e"></script>` goes before `</body>`, or at the end of the document if there is no `</body>`. When it is `None`, output is byte-identical to today, which a test asserts.

**Beacon (`collector/static/ken-beacon.js`):** about 150 lines, ES2017, no dependencies, no cookies.
- **Opt-out, then a no-op** if any of these holds: `navigator.globalPrivacyControl`, DNT = "1", `window.kenAnalyticsConsent === false`, or `localStorage.ken_internal === "1"` (staff self-exclusion).
- **`pv`:** 16 random bytes in hex from `crypto.getRandomValues`, per page load. It is held in memory only and never persisted.
- **Impression:** an `IntersectionObserver` at threshold 0.5. It fires once per link per `pv`.
- **Click:** `click` / `auxclick` on the link, sent immediately with `navigator.sendBeacon(Blob text/plain)`. This is a CORS-simple request, so there is no preflight and no ACAO header is needed.
- **Landing:** on click, the beacon writes `sessionStorage.ken_lc = {l, t}`. On the next page load it reads the value, clears it, and sends `lnd` if it is less than 30 minutes old. This fills the frozen `LinkMetrics.downstream_pageviews` without any identifier leaving the browser.
- **Batching:** impressions flush every 5 s and on `visibilitychange: hidden`, at most 50 per batch.
- **GA4 bridge:** if `window.dataLayer` exists, the beacon pushes `{event: "internal_link_impression" | "internal_link_click", ken_link_id}`. It never injects gtag.
- **Match modes:**
  - `attr` (default): `a[data-ken-link-id]`.
  - `href` (`data-ken-match="href"`, for a CMS that strips `data-*`): content-root links, at most 50, sent as `{h: href, a: anchorText}`. The server resolves them by (source path, href equals `plan.target_url`, anchor equals `plan.anchor_text`) among live plans of that source, which satisfies ADR-0014 §8.

**Wire schema (Pydantic, `extra=forbid`):**

```
{"v":1,"pv":"^[0-9a-f]{32}$","path":"/x (≤512, path only, no query)",
 "events":[{"t":"imp"|"clk"|"lnd","l":"<uuid>"} | {"t":…,"h":"≤2048","a":"≤120"}]}  (1–50 events)
```

**Collector validation, in order.** Every rejection still returns **204**, so the response is no validity oracle. Reasons are counted in memory and logged every 60 s.
1. Method and route check.
2. Body ≤ 4 KiB, and content type `text/plain`.
3. `Origin` is present and in `KEN_COLLECTOR_ALLOWED_ORIGINS`, which is set automatically to the sim base URL.
4. Bot and prefetch drop:
   - the UA regex list from policy, including `KenInterlinkBot`, headless, bot/crawl/spider and lighthouse;
   - `Sec-Purpose` / `Purpose: prefetch`;
   - `Sec-GPC: 1`.
5. Rate limit: a token bucket keyed by HMAC-SHA256(an in-memory random salt rotated daily, client IP), 120 events per minute per key plus a global cap. Neither the key nor the salt is ever persisted.
6. Schema check.
7. The link must resolve to a **live** plan through a `LiveLinkIndex`, refreshed every 30 s:
   - the plan has an applied `deployment_items` row and status in `LINK_ON_PAGE`, and is not rolled back;
   - `path` equals the path of `plan.source_url` (`imp`/`clk`) or of `plan.target_url` (`lnd`).
8. Insert with `ON CONFLICT (dedupe_key) DO NOTHING`, where `dedupe_key = sha256(pv|type|plan_id)`. `pv` itself is never stored.

**Pseudonymity.** Each stored row is (event type, plan, UTC date, receive time, experiment). It has no IP, UA, `pv`, referrer, query string, cookie or user id, and a schema test asserts the exact column set. IP and UA exist only in memory during the request. The only identifier-like value in the browser is a random id that lasts one page load.

**Production first-party.** Ken's own collection exports **aggregated daily** JSONL, which we import. The raw stream stays at its source. Deploying this collector or beacon to the real site is ASK.

### 2. Migration `0008_analytics` (all CHECK constraints named `ck_*`, as in 0007)

| Table | Columns | Integrity |
|---|---|---|
| `experiments` | `id uuid` PK; `key text` UNIQUE `^[a-z0-9][a-z0-9_-]{2,63}$`; `description ≤500`; `unit text` CHECK `'source_page'`; `status` `draft\|active\|ended`; `created_by` (actor pattern); `created_at`, `updated_at`, `started_at`, `ended_at` | No DELETE. Status only draft→active→ended. Content immutable after insert (to_jsonb guard). |
| `experiment_assignments` | `id bigint identity`; `experiment_id` FK; `source_page_id` FK pages; `arm` `control\|treatment`; `assigned_by`; `assigned_at`; UNIQUE (experiment_id, source_page_id) | Append-only (`ken_reject_mutation`). Assignment only while draft, and at most one active experiment per page (both service rules). |
| `analytics_imports` | `id uuid` PK; `job_run_id` FK job_runs; `source` `ga4\|search_console\|first_party`; `dataset` `links\|pages\|queries`; `via` `api\|csv\|jsonl\|collector\|synthetic`; `synthetic bool`; `date_start`, `date_end` (end ≥ start, span ≤ 400 days); `source_timezone text`; `file_name` (basename) and `file_sha256` (`^[0-9a-f]{64}$`), both NULL for API; `sampled bool`; `status` `running\|succeeded\|failed`; `counters jsonb` (object); `error_code` (vocabulary); `triggered_by`; `started_at`, `finished_at` | `ken_analytics_import_guard()`: no DELETE; only status/counters/sampled/error_code/finished_at may change; frozen once finished. |
| `link_events` | `id bigint identity`; `event_type` `internal_link_impression\|internal_link_click\|internal_link_landing`; `plan_id` FK link_plans; `match_mode` `attr\|href`; `experiment_id` FK NULL; `experiment_arm` NULL; `event_date date` (UTC); `received_at timestamptz`; `dedupe_key` UNIQUE hex64 | UPDATE always rejected. DELETE only when `current_setting('ken.analytics_purge', true) = 'on'` (SET LOCAL by the retention job). Indexes (plan_id, event_date) and (event_date). |
| `link_metrics_daily` | PK (`source`, `metric_date`, `plan_id`); `impressions`, `clicks`, `landings` (int ≥ 0); `import_id` FK; `synthetic bool` | UPDATE rejected. DELETE only under SET LOCAL `ken.analytics_writer = 'on'`. |
| `page_metrics_daily` | PK (`source` ∈ {ga4, search_console}, `metric_date`, `page_id`); `impressions`, `clicks`, `position_sum double ≥0` (Σ position × impressions); `pageviews`, `sessions`; `import_id`; `synthetic` | Same as above. |
| `page_query_metrics_daily` | PK (`metric_date`, `page_id`, `query_hash` = sha256 hex); `query text ≤500`; `impressions`, `clicks`, `position_sum`; `import_id`; `synthetic` | Same as above. GSC only. Opt-in import (`--queries`), top 50 rows per page per day (policy). |

**Replace semantics.** An import of (source, dataset, [start, end]) is **authoritative for every key on those dates**. It runs in one transaction:
1. SET LOCAL writer;
2. refuse with `synthetic_real_mix` if rows with the opposite `synthetic` flag exist in that range;
3. DELETE that range;
4. INSERT the rows, sorted by natural key, with duplicate keys within one import **summed** (counter `merged_rows`);
5. finish the ledger row.

Re-importing the same input gives identical rows. Provider rows outside the requested range are rejected. End dates past the source's lag (GA4 today − 1, GSC today − 3) are refused, so partial days are never stored.

**Retention** (policy): raw `link_events` 90 days, purged only for dates already aggregated; daily aggregates 25 months; query rows 16 months. Purging is `make analytics-purge`.

**Deferred:** partitioning, until volume is measured.

### 3. Attribution model

The plan is the attribution root, and it is immutable. `attribution.py` computes `LinkAttribution` with one SQL query (no stored copy):

- **plan:** `id`, `status`, `anchor_text`, `anchor_source`, `relationship_type`, `reason_codes`, `supersedes`.
- **recommendation run:** `plan.run_id`. The opportunity is `reason_codes` ∋ `orphan_target` / `underlinked_target`, because the `opportunities` rows are replaced each run and cannot be referenced.
- **versions:** `engine_version`, `policy_version`, `ranking_policy_version`, `analysis_version`, `calibration_version`.
- **source:** `source_page_id`, `source_url`, `source_page_version`.
- **target:** `target_page_id`, `target_url`, `target_page_version`.
- **agent:** `agent_run_plans.run_id`, `verdict`, `model`, plus `agent_runs.graph_version`.
- **human:** the approving `review_actions` row (actor, created_at).
- **deployment:** the applying `deployment_items` (stage `applied`) → `deployments.id`, `cms_provider`, `applied_at`, `verified_at`.
- **rollback:** `rolled_back_at` = the `link_plan_transitions` row to `rolled_back`. This also covers per-plan REMOVE deployments.
- **experiment:** the assignment of `source_page_id` to an experiment that was active during the window.

**Live window:**
- **First-party:** `[applied_at, rolled_back_at)`. It is enforced twice: at ingest, and again when aggregating (events outside the window are dropped).
- **Imported daily data:** `[D(applied_at), D(rolled_back_at)]` inclusive. Rows outside it are dropped (`outside_live_window`).
- **Superseded, rejected or never-applied plans** were never on a page, so their events are rejected (`link_not_live`). An edited plan's outcome reaches the original recommendation through `supersedes`.

**Day boundaries are source-local:**
- GA4: `KEN_GA4_TIMEZONE`, the property's time zone;
- GSC: America/Los_Angeles;
- first-party: UTC.

`D(x)` converts with `zoneinfo`.

**Before/after (page level, GSC and GA4, for the target and also the source).** With N = 28 days (policy):
- `D0` = D(applied_at), which is excluded as a partial day;
- pre = `[D0−N, D0−1]`;
- post = `[D0+1, min(D0+N, D(rolled_back_at)−1, last covered date)]`.

**Statistics:**
- days with coverage, totals and per-day means;
- CTR = clicks / impressions;
- average position = Σ position_sum / Σ impressions;
- the delta and ratio of the means, shown only when both windows have at least 14 covered days;
- `site_trend_ratio`: the same windows over all pages with data in both, shown as context and never used as an adjustment.

**Confounder flags:** `overlapping_changes` (another plan applied to the same page inside either window), `content_changed` (a new `page_versions` row in the windows), `post_window_truncated`, `rolled_back_in_window`, `insufficient_data`, `sampled_data`, `synthetic_data`.

**What we do NOT claim:** causal lift, statistical significance, ranking impact, or revenue. The UI copy says "observed change, not a causal effect". Seasonality, Google updates and sitewide trends are named as confounders. Causal analysis through experiments is Phase 8.

**`measured` transition.** The aggregation job moves `verified → measured` (`system:analytics`) under the ADR-0013 per-source xact lock and the plan row lock. It does so when `D0 + measurement_window_days` (28) ≤ the last date covered by a **non-synthetic** link-level import (first-party or GA4). This is the only plan mutation analytics performs. Synthetic data never changes plan state.

**Experiments (minimal, CLI-only).** The unit is the source page. `SqlExperimentGuard.locked()` is true when the source has a `control` assignment in an active experiment. It replaces `NoExperiments` in the pipeline and in review. Lift analysis is Phase 8.

### 4. Provider contracts (additive changes to `analytics.py`, non-breaking)

1. `PageMetrics`: add `pageviews: int = Field(0, ge=0)` and `position: float | None = Field(None, ge=1)`.
2. New `QueryMetrics(page_id, source, range, query ≤500, impressions, clicks, position)`.
3. New `FetchStats(rows_read, unresolved_url, unresolved_link, ambiguous_url, other_row, rejected_invalid, sampled: bool)`.
4. New `AnalyticsBatch(link_rows, page_rows, query_rows, stats)`.
5. New `AnalyticsBatchProvider(AnalyticsProvider, Protocol)`: `async fetch_batch(*, date_range, datasets: frozenset[Dataset], max_query_rows_per_page_day: int) -> AnalyticsBatch`. It is keyword-only and runtime-checkable. Every Phase 7 provider implements it, with `fetch_link_metrics` / `fetch_page_metrics` as thin filters over it.
6. New `PageUrlResolver` protocol, injected at construction and not in method signatures: `resolve(url) -> PageId | None`.
7. Docstring rule: Phase 7 providers return **daily** rows (`range.start == range.end`); otherwise the importer rejects the row with `non_daily_row`. `link_id` is `str(plan_id)`. Provider `ctr` is ignored and recomputed.
8. `MetricSource.CSV` is kept but never stored. CSV is a transport, and rows carry their origin.
9. New typed `AnalyticsUpdatedPayload`, plus the read models and `AnalyticsReader` in §6.

`AnalyticsProvider` itself is unchanged.

**Providers** (`services/ken_core/providers/analytics/`, built by `build_analytics_provider(settings, *, name, resolver, file=None, transport=None)`, which mirrors the CMS factory `match`):

| Provider | What it does |
|---|---|
| `mock` | Rows from fixtures or constructors. Also the vehicle for synthetic data. |
| `csv` / `jsonl` | UTF-8 with a header, ≤ 50 MB / 1 M rows, sha256 recorded. Exact headers, unknown columns rejected, dates `YYYY-MM-DD` or `YYYYMMDD`. Bad lines are counted and never echoed. |
| `ga4` | `POST {base}/v1beta/properties/{id}:runReport`. Links: dims `date`, `eventName`, `customEvent:ken_link_id`; metric `eventCount`; `eventName` inListFilter on the 3 event names. Pages: dims `date`, `hostName`, `pagePath`; metrics `screenPageViews`, `sessions`. `limit` 100000, offset paging, `orderBys` date then dimension. `(other)` rows are counted as `other_row`, and `samplingMetadatas` sets `sampled`. **We never send link ids or URLs to Google** (no id filters): only the property id and dates. |
| `gsc` | `POST {base}/webmasters/v3/sites/{urlencoded site}/searchAnalytics/query` with `{startDate, endDate, dimensions: ["date","page"(,"query")], type: "web", dataState: "final", rowLimit: 25000, startRow}`. Response keys: `clicks`, `impressions`, `position`. |
| `stored` (name `first_party`) | Implements the frozen read protocol over `link_metrics_daily`, for Phase 8 and 9 consumers. |

**CSV formats:**
- `ga4_link_daily`: `date,ken_link_id,event_name,event_count`
- `ga4_page_daily`: `date,host_name,page_path,screen_page_views,sessions`
- `gsc_page_daily`: `date,page,clicks,impressions,ctr,position`
- `gsc_query_daily`: `date,page,query,clicks,impressions,ctr,position`

**First-party JSONL v1.** The header line is `{"format":"ken-link-metrics","version":1,"exported_at","generator","timezone":"UTC"}`. Each following line is `{"date","link_id","impressions","clicks","landings"}`.

**URL → page.** `InventoryUrlResolver` is built once per import:
1. drop the query string and fragment, then normalise with `normalize_url`;
2. key on (host, path), ignoring the scheme, over `pages.canonical_url` first, then `known_urls.page_id`;
3. a key that maps to two pages becomes `ambiguous_url`, and an unmatched one becomes `unresolved_url`;
4. up to 100 unresolved paths (no query strings) are kept in `counters` as a sample.

**Google auth: recommend a pre-obtained OAuth access token, read from `KEN_ANALYTICS_GOOGLE_TOKEN_FILE`** at the start of every import. The operator mints it with read-only scopes, `analytics.readonly` and `webmasters.readonly`, for example with `gcloud auth print-access-token`, or a cron refresher that rewrites the file.
- **Why not `google-auth`:**
  - no new dependency (signing service-account JWTs needs `cryptography`, which is not in the lock file);
  - no long-lived private key held by the app;
  - every credential act stays an explicit operator step.
- **Cost:** tokens last about 1 hour, so unattended scheduled imports need an external refresher.

**Gating:** all I/O is refused before it starts unless `KEN_ANALYTICS_GOOGLE_ENABLED=true` (ASK) or the base URL is loopback (fixture server). The token is never logged.

**Retries:** 429/5xx get a bounded backoff of 3 tries. A 401/403 becomes the fixed code `google_auth_failed`.

### 5. Services, jobs, CLI

`ken_services/analytics/` contains `urls.py`, `importer.py`, `aggregate.py`, `attribution.py`, `windows.py`, `measure.py`, `experiments.py`, `synthetic.py`, `retention.py`, `reader.py` and `cli.py`. Each run is a `run_job(kind=ANALYTICS_IMPORT)`: one kind lock, the reaper applies, and `job.completed` is emitted. The `config_snapshot.mode` is one of `import | aggregate | purge | synthetic`.

**Commands:**
- `make analytics-import SOURCE=ga4|gsc|csv|jsonl DATASET=links|pages|queries START= END= [FILE=] [ARGS=--queries]`
- `make analytics-aggregate [START= END=]`: first-party `link_events` → `link_metrics_daily` for the default trailing 3 days (late events), then the `measured` sweep.
- `make analytics-purge`
- `make analytics-demo-seed ARGS="--seed 7"`
- `make analytics-sim-serve`
- `make experiment-create|experiment-assign|experiment-start|experiment-end`

**Counters:** `rows_read`, `rows_written`, `merged_rows`, `unresolved_url`, `unresolved_link`, `ambiguous_url`, `outside_live_window`, `outside_range`, `non_daily_row`, `other_row`, `rejected_invalid`, `plans_measured`.

**`analytics.updated`** is emitted once per succeeded import or aggregation, in the same transaction:
- `subject_type="analytics_import"`, `subject_id=str(import_id)`, `emitted_by="ken-analytics"`, `correlation_id=job_run_id`, `event_version` 1;
- payload `AnalyticsUpdatedPayload{import_id, job_run_id, source, dataset, via, synthetic, date_start, date_end, rows_written, rows_unresolved, rows_rejected, links_affected, pages_affected, plans_measured}`.

Failed runs emit only `job.completed`. The collector emits no events.

**Policy:** `policies/analytics/analytics.v1.yaml` (versioned) holds:
- the windows (28 days, 14-day minimum) and `measurement_window_days`;
- the source lag days and the `bot_ua_patterns`;
- the collector limits and the rate-limit values;
- the retention periods and the top queries per page per day.

**Settings:** `analytics_policy_path`, `analytics_google_enabled`, `analytics_google_token_file`, `ga4_property_id`, `ga4_timezone`, `gsc_site_url`, `analytics_ga4_base_url`, `analytics_gsc_base_url`, `analytics_google_timeout_s`, `collector_host`, `collector_allowed_origins`, `collector_allow_remote`, `analytics_import_max_file_bytes`. Only non-secret values go into `public_settings`.

### 6. API (GET-only, all `Permission.READ`; no new permission)

Imports stay **CLI-only**, like agent runs (ADR-0013 §7). A UI button must never trigger Google egress, which is ASK.

| Route | Query | Response |
|---|---|---|
| `GET /analytics/summary` | `start`, `end` (default: the 28 days ending at the latest data date; span ≤ 400) | `AnalyticsSummary{range, sources: [SourceFreshness{source, latest_date, last_import_id, last_status}], totals_by_source: {source: MetricTotals}, daily: [DailyTotals], live_links, links_with_data, top_links: [LinkPerformanceView] (≤10), synthetic: bool}` |
| `GET /analytics/links` | `start`, `end`, `source`, `source_page_id`, `target_page_id`, `deployment_id`, `plan_status`, `sort=clicks\|impressions\|ctr\|landings`, `order`, `limit ≤100`, `offset` | `ListResponse[LinkPerformanceView{plan_id, anchor_text, source_url, target_url, plan_status, applied_at, rolled_back_at, totals: {source: MetricTotals}, synthetic}]` |
| `GET /analytics/links/{plan_id}` | `start`, `end` | `LinkPerformanceDetail{attribution: LinkAttribution, live_window, series: {source: [LinkDailyPoint]}, totals, target_impact: [PageWindowComparison], source_impact: [PageWindowComparison]}`. 404 if the plan is unknown; empty series if it was never deployed. |
| `GET /analytics/pages/{page_id}` | `window_days` (7–90) | `PageImpactView{page_id, url, series: {source: [PageDailyPoint]}, changes: [PageWindowComparison & role source\|target, plan_id]}` |
| `GET /analytics/pages/{page_id}/queries` | `start`, `end`, `limit ≤100` | `ListResponse[QueryRow]` |
| `GET /analytics/imports`, `GET /analytics/imports/{id}` | `source`, `status`, `limit`, `offset` | `AnalyticsImportView` |
| `GET /analytics/experiments` | — | `ListResponse[ExperimentView]` |

**Response models:**
- `MetricTotals{impressions, clicks, landings, ctr: float | None}`. `ctr` is None when impressions = 0.
- `PageWindowComparison{page_id, source, anchor_date, pre: WindowStats, post: WindowStats, delta_per_day, ratio, site_trend_ratio, flags: [ConfounderFlag], synthetic}`.

### 7. UI (Next.js, server-rendered, no chart dependency)

- **`/analytics`:**
  - KPI tiles: live links, impressions, clicks, CTR, landings;
  - a source and range GET form;
  - a daily SVG sparkline with a data table under it;
  - the top links table, source freshness and recent imports.
- **`/analytics/pages/[pageId]`:** paired SVG bars (pre/post per-day mean) per change, with confounder chips and a fixed "observed change, not causal" note.
- **Plan detail:** a "Performance" panel when the plan was ever applied: the attribution chain, the live window and per-source totals and series.
- **Deployment detail:** a per-item performance table (`/analytics/links?deployment_id=`).
- **Inventory page detail:** a link to page impact.
- **Navigation:** an "Analytics" entry.
- **Charts:** hand-written `Sparkline` and `BarPair` SVG components with `<title>` and table fallbacks. No new dependency is justified.
- **Empty states:**
  - no imports yet: the exact `make` commands;
  - a live link with no events: "Live since … — no events recorded";
  - insufficient window data: "Pre-window has 9 of 28 days";
  - CTR shows "—" when impressions are 0.
- **Synthetic data:** a persistent banner, plus a badge on every synthetic series.

### 8. Demo seed

`make analytics-demo-seed` runs `synthetic.py`:
- It walks every plan that was ever applied and every day of its live window.
- It uses `random.Random(f"{seed}:{plan_id}:{date}")`. A string seed is hashed with sha512, so it does not depend on PYTHONHASHSEED.
- Base traffic per page comes from sha256(page_id). CTR falls in 0.5–4 %, and landings are about 85 % of clicks.
- GSC and GA4 page series cover the pre and post windows with a small seeded drift.
- It feeds `mock` → the normal importer with `via=synthetic, synthetic=true`.
- It refuses `staging`/`production` and refuses to mix with real rows. It is repeatable byte for byte.
- `make demo` can additionally drive the real collector path with a Python "synthetic visitor" that posts beacon payloads to the loopback collector.

### 9. Tests and evals

- **Contract:**
  - the additive models and protocol;
  - keyword-only async `fetch_batch`;
  - the `AnalyticsUpdatedPayload` shape;
  - no `/e` or collector route on the API app;
  - every `/analytics/*` route is GET-only;
  - the OpenAPI snapshot is regenerated.
- **Migration:**
  - upgrade and downgrade round trip;
  - append-only and purge or writer gating (DELETE without the setting fails);
  - the import ledger freezes;
  - the `link_events` column-set assertion (no PII columns).
- **Import:**
  - the same file twice gives identical rows;
  - an overlapping range replaces;
  - summing of duplicate keys;
  - `synthetic_real_mix` refusal;
  - end date past the lag refused;
  - non-daily and out-of-range rows rejected;
  - unresolved, ambiguous and query-stripped URLs;
  - time-zone day boundaries.
- **Attribution and windows:** a golden scenario in `evals/analytics/attribution/`, with plans that are applied, rolled back mid-window, per-plan REMOVE, superseded, never deployed, overlapping on the same source, plus a content change. The expected `LinkAttribution`, windows, flags and `measured` transitions are asserted exactly, and synthetic data never measures.
- **Collector:**
  - oversize, wrong content type, missing or foreign Origin;
  - bot UA, prefetch, GPC;
  - rate limit;
  - unknown, not-live and rolled-back plan;
  - path mismatch;
  - href-mode resolution;
  - duplicate `pv`;
  - always 204;
  - listener nonce and port guards;
  - no IP or UA in logs or the DB.
- **Beacon:** vitest + jsdom (already devDependencies) with a stub `IntersectionObserver`, `sendBeacon` and storage: opt-outs, one impression per `pv`, click, landing hand-off, the dataLayer push and payload bounds.
- **Sim site:** byte identity when `beacon_src=None`; content-hash and verification parity with the beacon injected.
- **GA4/GSC:** `httpx.MockTransport` asserts the exact request bodies, paths, paging, `(other)` and sampling handling, and 401/429; refusal before I/O when disabled; the token never appears in logs. Google is never called.
- **API:** RBAC matrix (viewer 200, anonymous 401); the reader runs on the READ ONLY session; parameter validation (span, limits).
- **Web:** the screens, the empty states and the synthetic banner.

### 10. Workstreams (frozen interfaces first)

**W0, orchestrator, before fan-out** (single owner of these files):
- `contracts/python/ken_contracts/analytics.py`
- `migrations/versions/0008_analytics.py` and `services/ken_core/db/models/analytics.py`
- `services/ken_core/config.py`
- `policies/analytics/analytics.v1.yaml`
- `apps/api/ken_api/routers/analytics.py` (frozen signatures) and `deps.py`
- `Makefile`, `contracts/openapi/openapi.json`, `apps/web/lib/types.gen.ts`
- `docs/CONTRACTS.md` §19 and this ADR
- frozen function signatures for `attribution.live_windows(session, plan_ids) -> dict[UUID, LiveWindow]` and `LiveLinkIndex`

**W1, backend:**
- `services/ken_core/providers/analytics/*`
- `services/ken_core/db/repositories/analytics.py`
- `services/ken_services/analytics/*` except `collector/`
- the `SqlExperimentGuard` wiring in `pipeline.py` / `review/service.py` (one-line injection only)
- its tests and `evals/analytics/`

W1 could split into W1a (pure providers and CSV) and W1b (importer, aggregation, reader).

**W2, collector:**
- `services/ken_services/analytics/collector/**`, including `static/ken-beacon.js`
- the `beacon_src` change in `services/ken_services/deployment/sim_site.py`
- the beacon vitest file
- collector tests

**W3, API:** router bodies, the RBAC matrix extension, API integration tests.

**W4, frontend:**
- `apps/web/app/analytics/**` and `components/analytics/**`
- `lib/analytics.ts`, plus the analytics sections of `lib/api.ts` / `lib/api-client.ts`
- the plan and deployment detail panels and the nav

### 11. Risks and ASK items for the user

1. **ASK: Google credentials.** The GA4 property id and time zone, the GSC property (`sc-domain:` or URL-prefix), and a read-only OAuth token and how it is refreshed. Enabling `KEN_ANALYTICS_GOOGLE_ENABLED` is external egress. Only the property id and dates are sent.
2. **ASK / legal: consent.** The beacon uses `sessionStorage` and sends behavioural events, which ePrivacy, GDPR and India's DPDP Act may treat as analytics needing consent. Ken must choose its CMP integration (`window.kenAnalyticsConsent`). Putting the beacon or collector on the real site is a live deployment.
3. **ASK: GA4 setup.** Register the event-scoped custom dimension `ken_link_id` (data only accrues from registration onward), and add a GTM/gtag rule forwarding the dataLayer events. Cardinality may cause `(other)` rows.
4. **Decision: production first-party.** Where Ken hosts collection. Until that is decided, the aggregated JSONL import is the contract.
5. **Decision:** adopt the automatic `verified → measured` transition, and the minimal source-page experiments with a holdout guard. The alternative is to defer both to Phase 8 with nullable `experiment_id`.
6. **Accepted limitations:**
   - no real-browser E2E for the beacon (Playwright is still deferred);
   - collector downtime cannot be detected as missing coverage;
   - GSC data lags about 3 days and holds 16 months;
   - GA4 thresholding and sampling;
   - attribution is correlational only;
   - `href` mode relies on anchor and href stability.
7. **Architecture flag:** the collector is a new network listener, but not a new infrastructure service: same Python and the same PostgreSQL. A production deployment would want an INSERT-only DB role on `link_events`.

### 12. Amendments from the pre-fan-out architecture review (binding)

Verdict: **APPROVED WITH CHANGES.** The changes below are binding, and they supersede §1–§11 where they conflict.

#### 12.1 Corrections against the code

Confirmed:
- `JobKind.ANALYTICS_IMPORT` (`contracts/python/ken_contracts/jobs.py:44`) and `EventName.ANALYTICS_UPDATED` (`events.py:78`).
- `verified→measured` is the only way into `measured`, and the actor must be `system` (`recommendations.py:153,205`, and the copy in 0007 `PLAN_TRANSITIONS`).
- `LINK_ON_PAGE` includes `measured` (`services/ken_services/deployment/service.py:176`). Rollback tx2 re-reads status against `LINK_ON_PAGE` (about line 1716), so a concurrent `measured` move does not break rollback.
- `ExperimentGuard` is at `recommendations.py:499` and `NoExperiments` at `recommendations/filters.py:108`. They are injected at `pipeline.py:867/891` and `review/service.py:308/316`.
- `opportunities` is replaced per run (`0004` docstring, `replace_opportunities`).
- Inventory normalisation (`_REMOVED_TAGS`) and `source_map.RAW_TEXT_TAGS` both drop `<script>`.

Wrong or incomplete:
- **a. Live-window start.** "Applied `deployment_items` row (stage applied)" misses `already_applied`: a CMS no-op or replay records that stage while the plan still becomes `deployed` (`service.py:1061-1064`). Use the `link_plan_transitions` rows instead: `to_status='deployed'` for the start and `'rolled_back'` for the end.
- **b. href mode compares against the wrong value.** It matches `plan.target_url`, but the written href is `deployment_items.href` (the `LinkDeployedPayload.href` logic). Compare with `href_rules.hrefs_equivalent`.
- **c. `preserves_data_attributes` location.** It lives on `SimulatedCMSProvider` (`ken_core/providers/cms/simulated.py:284`) and the mock, not on `sim_site`. `render_page` serves full documents verbatim.
- **d. Query strings.** `sim_site._path_of` keeps the query string, but the wire `path` drops it. Compare normalised paths only, on both sides.
- **e. `failed` plans.** `LINK_ON_PAGE` includes `failed`, so the `LiveLinkIndex` will accept events for failed plans. State that this is intended (the link may be on the page) and that measuring is only ever from `verified`. A plan whose verification stays inconclusive stays `deployed` and never measures: document that.
- **f. Redundant ledger status.** `analytics_imports.status` duplicates `job_runs`. The reaper only fixes `job_runs`, and since the import is one transaction a `running` row is never visible anyway.
- **g. Doc conflict to record, not choose.** AUTONOMY_AND_PERMISSIONS.md lists `READ_ANALYTICS: ALLOW`, while your directive makes real Google calls ASK. Have the ADR read it as: reading stored analytics is ALLOW; Google egress is ASK ("sending data externally").

#### 12.2 Scope: keep / cut / defer

| Item | Call | Reason |
|---|---|---|
| Experiments tables, guard, experiment CLI | **DEFER to Phase 8** | IMPLEMENTATION_PLAN Phase 7 is "link IDs, first-party events, GA4/GSC, CSV, dashboards". Phase 7 has no consumer for experiments, and arm semantics (holdout vs policy A/B) belong to the policy EXPERIMENT stage. Keep `NoExperiments`. PRD 7.12 "experiment ID" is met by deriving it (amendment 2). |
| href match mode | **KEEP, narrowed** | ADR-0014 §8 says attribution must not depend on `data-*`. See 2b. |
| `page_query_metrics_daily`, `QueryMetrics`, `--queries`, queries route | **DEFER to Phase 8** (search learning) | GSC keeps 16 months, so nothing is lost by waiting. |
| Retention / `analytics-purge` | **DEFER the command** | Keep the gated-DELETE trigger so adding it later needs no migration. Rows hold no PII. |
| Rate limiter: HMAC plus a daily salt | **SIMPLIFY** | The key is never persisted, so hashing adds nothing. Use a bounded per-IP fixed-window map plus a global cap. |
| 4 CSV formats plus JSONL plus 2 date formats | **CUT to 2 canonical CSVs** | `ken_link_daily: date,source,link_id,impressions,clicks` and `ken_page_daily: date,source,url,impressions,clicks,position,pageviews,sessions`, with `YYYY-MM-DD` only. Production first-party export uses `ken_link_daily`. |
| Landing events (`lnd`, sessionStorage, `landings`) | **CUT** | Adds a storage-based tracking and consent surface for little value. `downstream_pageviews` stays 0. |
| Automatic `measured` transition | **KEEP**, with a snapshot (amendment 7) | It is the documented Phase 7 role of the status. |
| Routes | **5 routes** | summary, links, links/{id}, pages/{id}, imports (list only). |
| `make analytics-collect-serve` | **CUT** | A standalone collector only matters for the production collector, which is ASK. |
| GA4 dataLayer bridge, staff opt-out, `site_trend_ratio` | **KEEP** | Small, and the bridge is the only route to link-level GA4 data. |

#### 12.3 Security and privacy (binding)

Good as drafted: always 204, no CORS preflight, no pv/IP/UA persisted, GPC/DNT handled on both client and server, loopback binding with nonce-verified startup, CLI-only egress, and a test that the API app has no collector route.

Missing:
- **Body limit.** Enforce 4 KiB on the bytes actually streamed, not on `Content-Length`, and abort chunked bodies past the cap.
- **Bounded rate-limit map.** Without a cap and LRU eviction, IPv6 rotation is a memory DoS. Take the client IP only from the ASGI peer (`proxy_headers=False`).
- **Origin.** Exact match on scheme, host and port. Treat `null` as foreign.
- **`received_at`.** Truncate to the minute; full precision allows a join with web-server logs.
- **First-party counts are spoofable.** Origin and pv are attacker-chosen. Document this so Phase 9 never graduates a decision class on first-party data alone.
- **Token exfiltration.** When Google is enabled, the base URL must be `https` and the host exactly `analyticsdata.googleapis.com` or `www.googleapis.com`. Loopback fixtures are allowed only in `local`/`ci`, and then the token file is never read. Set `follow_redirects=False` and `trust_env=False`. The token never appears in `config_snapshot`, counters or errors. Require 0600 permissions on the token file.

#### 12.4 Integrity

- Use one transaction-local setting, `ken.analytics_writer`, through `set_config(…, true)`, with one shared guard function. Test that it does not leak across pooled connections.
- The replace step's mix check must also compare `via`, not just `synthetic`. Otherwise the collector aggregation and a production first-party CSV silently overwrite each other.
- The single ANALYTICS_IMPORT kind lock serialises import, aggregate and measure. That is good.
- Measure versus deploy/rollback: take `pg_try_advisory_xact_lock(deploy_lock_key(source))` and skip the source if it is busy (session and transaction advisory locks share one keyspace), then `lock_source_plans`, then the row lock, then `expected_from=VERIFIED`.
- Idempotency is adequate: the collector deduplicates, imports replace, and the transition is guarded.

#### 12.5 Phase 8/9 readiness

There are two gaps:
1. **Nothing records what was measured.** Replace semantics delete the inputs behind a measurement, so Phase 8 outcome learning and Phase 9 `positive_outcome_rate` would be unreproducible.
2. **Experiment columns stamped on events** would compete with the assignment table Phase 8 adds.

Amendments 2 and 7 fix both. Decision-class features (relationship type, `anchor_source`, `reason_codes`, versions) are already available through the plan join.

#### 12.6 Required changes

1. Close Phase 6 (commit and tag) before the W0 freeze.
2. Drop `experiments`, `experiment_assignments`, `experiment_id`, `experiment_arm`, `SqlExperimentGuard`, the experiment make targets and `/analytics/experiments`. Add the ADR rule: experiment attribution is always derived (source-page assignment × active interval), never stamped on events. Phase 8 adds the tables additively.
3. Define the live window from the `link_plan_transitions` rows `deployed` and `rolled_back`. The `LiveLinkIndex` and attribution use those, not the `deployment_items` stage.
4. href mode resolves (normalised source path, `hrefs_equivalent(deployment_items.href)`, exact anchor) to the one matching live plan of that source. Anything else is dropped as `ambiguous_link`.
5. Add `link_events.synthetic bool NOT NULL`, set by the collector from its configuration: true when every allowed origin is loopback. The client cannot set it. Aggregation carries it through to `link_metrics_daily.synthetic`.
6. `analytics_imports`: drop `status`, `counters` and `error_code`. Insert the row once, on success only, in the data transaction, and make it immutable with `ken_reject_mutation`. Add `analytics_policy_version` and `unresolved_sample jsonb` (at most 100 paths). Failures live only in `job_runs`.
7. New append-only `link_measurements`: `plan_id` PK and FK; `measured_at`; `analytics_policy_version`; `window_start`/`window_end`; `link_totals`, `target_comparison`, `source_comparison` and `flags` as jsonb; `import_ids` as a jsonb array. Validate it through a Pydantic `LinkMeasurement`. Write it in the same transaction as `verified→measured`. Synthetic data never measures (accepted: the local demo never shows `measured`). Run the measure sweep after every successful link-level import or aggregation.
8. Replace mix refusal checks both `synthetic` and `via` (`synthetic_real_mix`, `via_mismatch`). An override is possible only with the explicit `--replace-via`, which is recorded on the ledger.
9. Make the scope cuts and deferrals in §3 (queries, purge command, landing, JSONL and native CSVs, collector-only target, `imports/{id}`). `AnalyticsBatchProvider.fetch_batch` loses `max_query_rows_per_page_day`. The GA4 event filter has 2 names.
10. Make the collector hardening in §4 binding: streamed body cap, bounded per-IP map, ASGI peer IP, exact Origin, `received_at` truncated to the minute.
11. Make the Google egress guards in §4 binding: host allowlist, https, no redirects, no environment proxies, token permission check, token never read for loopback, token never in snapshots.
12. Implement the measure lock order from §5, and skip sources whose deployment is `rolling_back`.
13. Put the READ_ANALYTICS-versus-egress interpretation (2g) in the ADR, and state that the collector adds no compose service. Hosting it in production remains ASK.

#### 12.7 Risks

- First-party data can be inflated.
- GA4 `(other)` rows and thresholding.
- Plans with inconclusive verification never measure.
- `overlapping_changes` will flag most batch deployments, so Phase 8 should group by deployment.

#### 12.8 Orchestrator decisions on the amendments

- All 13 required changes are accepted.
- **Experiments are deferred to Phase 8.** The PRD §7.12 "experiment id" is derived (source-page assignment × active interval) and never stamped on events. `NoExperiments` stays wired in.
- **Landing events, query metrics, the purge command, JSONL import and the standalone collector target are deferred.** The collector runs only in `make analytics-sim-serve` (local) until the user approves a production collector.
- **CSV fallback is two canonical formats:** `ken_link_daily` and `ken_page_daily`, with ISO dates.
- **Reading stored analytics is ALLOW** (AUTONOMY_AND_PERMISSIONS `READ_ANALYTICS`). **Any Google egress is ASK** (sending data to an external service). It is off by default (`KEN_ANALYTICS_GOOGLE_ENABLED=false`), and only the CLI can trigger it.

#### 12.9 Decisions settled during the contract freeze (W0)

Resolved while freezing the contracts; these are now binding alongside §12.

1. **The measure sweep runs inside the import transaction**, taking per-source try-locks and skipping busy sources. Only then can `analytics.updated.plans_measured` be exact, because the event is emitted in the ledger's own transaction.
2. **Source restrictions are enforced in the schema.** `link_metrics_daily.source` accepts only `ga4` and `first_party`, because Search Console has no link-level data. The ledger CHECKs also tie `via` to the source: `collector` only for first-party links, `api` only for Google.
3. **Ledger and freshness fields.** The ledger carries `rows_written` and `source_timezone`. `AnalyticsImportView.counters` comes from `job_runs`. `SourceFreshness` drops `last_status` (amendment 6 removed the status column) and gains `dataset`, `last_via` and `last_imported_at`.
4. **Google base URLs.** With egress enabled they must be the exact https hosts `analyticsdata.googleapis.com` and `www.googleapis.com`, and a token file is required. A loopback fixture URL is allowed only in `local` or `ci`, and only while egress is disabled.
5. **Collector origins.** A non-loopback collector *origin* also requires `KEN_COLLECTOR_ALLOW_REMOTE`, not just a non-loopback bind. This is stricter than §12.3.
6. **Window comparisons.** `PageWindowComparison` gains `role`, `plan_id` and `metric` (Search Console compares clicks, GA4 compares pageviews). `LinkPerformanceDetail` gains `range` and `measurement`.
7. **href matching.** Both hrefs are resolved against the plan's `source_url` before `hrefs_equivalent`. The beacon sends the raw `getAttribute('href')` value.
8. **`FetchStats.unresolved_sample`** was added so a provider can feed the ledger's sample directly.
9. **No new not-found errors.** An unknown plan or page reuses the existing typed errors (`PlanNotFoundError` → 404 `not_found`; `InventoryNotFoundError("page")` → 404 `page_not_found`), so `errors.py` is unchanged.
10. **`/settings/policies` is not data-driven.** Serving the analytics policy needed one additive edit in each of three places: `PolicyKind` (a closed Literal), the control-plane `policies()` builder, and the web app's `POLICY_KIND` label map. Accepted: the alternative was to hide a released policy document from the operator.

**Verified at the freeze** (orchestrator, independently of the implementing agent): the writer guard refuses every UPDATE even with the setting on, refuses DELETE without a transaction-local `ken.analytics_writer`, allows it with one, and does not leak the setting to the next transaction on the same backend; `link_events` holds exactly the eight pseudonymous columns, with no experiment columns; `received_at` must be minute-truncated; `verified → measured` is still the only route into `measured`; the ledger and measurements are append-only even for the writer; and the 0008 round trip plus `alembic check` are clean. 180 Phase 7 contract, schema, settings and route tests pass.

#### 12.10 Decisions settled during implementation (W1/W2/W4)

Resolved while building against the frozen contracts; binding alongside §12.

1. **The ledger row is inserted before the metric rows.** `link_metrics_daily.import_id` references `analytics_imports.id` and the FK is not deferred, so §2's prose order ("insert rows, then the ledger") is physically impossible. `rows_written` is computed first and the ledger row goes in complete. It is the same transaction, so nothing observable changes.
2. **`content_changed` compares `page_versions.fetched_at`, not `created_at`.** `created_at` records when a row happened to be written, so a backfill or re-ingestion would otherwise look like an editorial change on the day it ran.
3. **`LinkMeasurement.window_start`/`window_end` are UTC**, while each comparison inside the snapshot keeps its own source-local `anchor_date`: the contract has one window pair but per-source day boundaries.
4. **`config_snapshot.mode` is derived from `via`** (`collector`→`aggregate`, `synthetic`→`synthetic`, else `import`), so the frozen `RunImport` signature needed no extra argument.
5. **Aggregation's live-window drops are logged, not counted.** The frozen `FetchStats` has no field for them and `COUNTER_NAMES` is frozen.
6. **The CSV row cap is applied twice.** The factory lives in `ken_core`, which must not import `ken_services`, so it cannot read the policy: the provider takes the contract maximum as a structural cap and the importer enforces the configured `imports.max_rows` on top.
7. **Provider failures are a `FetchErrorCode` in `ken_core`**, mapped to `ImportErrorCode` by the importer, for that same layering reason.
8. **Read fan-out is bounded.** `page_impact` builds at most 50 comparisons, newest deployment first, so one busy page cannot turn a GET into unbounded query fan-out.
9. **One typed refusal was added** that the ADR implies but does not name: a non-CSV run carrying `file_sha256` is `invalid_request`, rather than a ledger CHECK violation surfacing as a generic failure.
10. **The collector reaches the simulated site through `add_allowed_origin()`.** The site's origin exists only once its socket is bound, and the collector must start first because the beacon tag needs its URL. A non-loopback addition still requires `KEN_COLLECTOR_ALLOW_REMOTE`, and `synthetic` is recomputed on every addition — never client-settable.
11. **Rate limiting charges events after parsing.** The policy caps events per minute, but the limiter runs before the body is parsed: a window may overshoot by at most one request (≤50 events). The alternative was parsing untrusted bodies before limiting them. LRU eviction gives an evicted address a fresh budget, so the **global** cap is what bounds address rotation.
12. **The beacon receives policy bounds as a one-line prelude** prepended to the untouched static file, so the policy stays the single source of truth, the file runs unmodified under vitest, and the served bytes are a pure function of (file, policy). The beacon clamps every value it reads.
13. **Packaging:** `pyproject.toml` declares `ken_services.analytics.collector = ["static/*.js"]`, because a wheel without the beacon would serve `GET /k.js` as a 500.
14. **A UI/CLI drift was caught at integration.** The dashboard's empty state printed `SOURCE=csv`, which argparse rejects: `SOURCE` is the row's **origin** (`ga4` | `gsc` | `first_party`) and passing `FILE` is what makes a run a CSV import. The copy is corrected and a test now pins any `SOURCE=` the UI prints to a value the CLI accepts. `SOURCE=first_party` without `FILE` exits 2 and points the operator at `make analytics-aggregate`, because first-party rows are produced by aggregation and never imported.

#### 12.11 QA/security review outcomes (Phase 7)

Verdict: **PASS WITH REQUIRED FIXES**, with no critical or high finding. The review probed by execution: 24 collector abuse classes (all answered 204 with byte-identical headers and an empty body, so the response is no validity oracle); an attempt to force identifying data into a log line through the user agent, origin, peer IP, path, a failing sink and a forced counter flush (only an error class and bare counts appeared); rate-limit steering through `X-Forwarded-For`, `X-Real-IP` and `Forwarded` (one peer got exactly one quota) and 200,000 rotating IPv6 addresses (held to the global cap); Google egress with the flag off (zero transport calls) and bad base URLs, including a lookalike host, refused at `Settings` construction; proxy environment variables ignored; the writer guard; and the migration round trip.

| Finding | Resolution |
|---|---|
| **M1. An import whose every row was rejected deleted the range and exited 0.** The replace DELETE ran before any row survived validation, so a wrong or corrupt file silently destroyed a range of `link_metrics_daily`. | **Fixed, not accepted.** An import that read rows but would write none is refused (`file_invalid`), the transaction rolls back and the stored rows survive. A genuinely empty source (`rows_read == 0`) still clears the range — that remains the deliberate way to do it. Three tests pin it: the QA reproduction, the deliberate clear, and a partially-valid import still replacing. |
| **M2. `list_links` had unbounded read fan-out.** It loaded every deployed plan and every daily row in the range, built every view, sorted in Python and then sliced, so one authenticated GET with a 400-day span was `O(live links × days)` rows in memory. | **Fixed.** `rank_deployed_plans` aggregates, ranks and pages in SQL, so at most `limit` rows return (the route caps `limit` at 100; the summary's top list stays `MAX_TOP_LINKS = 10`). The bound is now constant in the range length. This is the §12.10.8 bound for `list_links`, alongside `page_impact`'s `MAX_PAGE_CHANGES = 50`. |
| L3. The rate limiter charged only requests that passed the schema check, so invalid bodies cost nothing (600 invalid POSTs from one peer were never limited); and the body read had no deadline, so many small chunks under the cap held the coroutine open. | Fixed: every request reaching the limiter is charged one event immediately, with a valid batch charged the rest, and the body read has a timeout. Rejections remain 204 with identical headers. |
| L4. The `rolling_back` check was read once **before** the per-source try-lock loop, so measurement could race a rollback that committed its first transaction after that read (TOCTOU). | Fixed: the check moved inside the loop, after the try-lock succeeds, with a test pinning the ordering. |
| L5. Nothing pinned the two behaviours the review amendments introduced. | Added to the golden scenario: a plan deployed through a CMS no-op whose only item stage is `already_applied` (§12.1a), and a `deployed → failed` plan the collector accepts but which never measures (§12.1e). |
| I6. Valid events do a database insert while rejections return early, so **timing** still separates them even though the response does not. | Accepted and recorded: inherent to storing anything at all. |
| I7. An all-rejected import now writes no ledger row; the attempt is recorded only as a failed job run. | Accepted: the failure reason is `file_invalid` and the run is auditable. |

**Not verifiable here, by design:** real GA4 and Search Console calls (the code is asserted unable to reach them rather than tried), `make analytics-sim-serve` end to end (it needs a seeded `ken_cms_sim`; it exits cleanly and binds nothing without one), real-browser beacon behaviour (Playwright still deferred), and concurrent collector inserts under real load.


## Amendment (2026-10-27): service-account authentication

Section 12's recommendation (a pre-obtained access token in `KEN_ANALYTICS_GOOGLE_TOKEN_FILE`) stays supported. A
second source is now available: `KEN_ANALYTICS_SERVICE_ACCOUNT_FILE`, a Google service-account JSON key (mode 0600) for a
service account with read-only access to the GA4 property and the Search Console property. Setting both is refused.

* `ken_core/providers/analytics/service_account.py` signs an RS256 JWT (scope `analytics.readonly` for GA4,
  `webmasters.readonly` for Search Console) and exchanges it at `https://oauth2.googleapis.com/token` for a short-lived
  access token, cached in memory per scope and refreshed two minutes before expiry. The key is re-read at each refresh.
* The token URI in the key file is ignored (always the fixed Google host); the request is https, no redirects, no
  proxy, bounded 3-try backoff. A bad key file, a wrong mode, an invalid PEM or a non-service-account JSON is the fixed
  code `token_file_invalid`; Google refusing the assertion is `google_auth_failed`. No key material reaches a log, an
  error or a snapshot.
* Egress rules are unchanged: `KEN_ANALYTICS_GOOGLE_ENABLED` (an ASK action) must be on, and nothing but the property id and the
  dates is sent to the data APIs. Loopback fixtures never read the key.
* New dependency: `cryptography` (Apache-2.0 OR BSD-3-Clause), used only for the RS256 signature.
