# Phase 1 Report — Content Inventory

**Status: complete.** Phase 2 has **not** been started.
**Date:** 2026-10-13 → 2026-10-14
**Design:** [docs/adr/ADR-0009-content-inventory.md](docs/adr/ADR-0009-content-inventory.md)
**Git:** two local commits on top of Phase 0 (`66772b8` contract freeze, `f7252e4` implementation),
plus the uncommitted review fixes, which are committed with this report. Nothing has been pushed;
there is no remote.

## 1. What was built

The system can now take stock of a website's content reliably, before any recommendation logic
exists:

```
source  ──▶  deterministic normaliser  ──▶  versioned PostgreSQL inventory  ──▶  read-only API  ──▶  web UI
```

| Capability | Result |
|---|---|
| **Source abstraction** | One `ContentSourceProvider` protocol yields `SourceDocument` / `SourceIssue` records in a deterministic order. Persistence never knows which source produced a document. |
| **Public web crawler** | Polite and domain-scoped. RFC 9309 robots.txt is always honoured, scope is checked on every redirect hop, and requests are GET only with no cookies or credentials. Rate limit, concurrency, timeout, size, depth, page-count and query-trap caps are enforced. Tested **only** against local fixtures. **No real site is configured; none was guessed.** |
| **File import** | A documented JSONL format: a header line, then one page per line. Used for fixtures, offline bootstrap and future CMS exports. |
| **CMS source** | A complete adapter over a **read-only** `CMSInventoryReader`. Only a mock reader exists, and nothing connects to a real CMS. |
| **Normalisation** | Pure function, no LLM. It covers: URL, canonical with a status, title with a recorded source, description, language (from markup and header only), publication state, page type, dates, robots directives, body HTML, body text quoted exactly as a browser renders it, headings, and body links with block identity. The content hash covers content fields only. |
| **Versioning** | `pages` (stable UUID identity, unique canonical URL, optional CMS id) and **immutable** `page_versions` and `existing_links`, enforced by a DB trigger. A new version is written only when the hash changes, and **unchanged content writes nothing**. |
| **Existing links** | Every body link stores: href, resolved URL, type (internal / external / fragment / non-http / invalid), anchor text and its source, rel, block id/kind, occurrence index, context text and context hash. Resolution (resolved / broken / unresolved / not applicable) is computed at **read time** through `known_urls`, so redirects and aliases resolve and a 404/410 shows as broken. |
| **Run model** | Each run records source, status, start and end times, 11 counters with an enforced invariant, a secret-free configuration snapshot, and per-URL issues. A failed run stores a safe reason (exception class only). A single-writer advisory lock refuses a concurrent run. |
| **Events** | `page.created`, `page.updated`, `page.deleted` (a 404/410 observation, emitted on the transition only) and `crawl.completed` (every run of every source, including failed runs). **No new event names.** |
| **API** | Nine read-only `GET /inventory/*` routes over an `InventoryReader` protocol. No route can write or start a crawl. |
| **UI** | Inventory, page detail (versions, links, inbound links, text) and run list/detail screens. `body_html` is never rendered as HTML, and unsafe hrefs are never clickable. |

## 2. Schema and migrations

**Migration `0002_content_inventory`** (round trip verified: `upgrade head → downgrade base →
upgrade head` on `ken_interlink_test`; `plpgsql` is the only extension):

| Table | Purpose |
|---|---|
| `ingestion_runs` | One row per crawl/import, with 11 counters, a config snapshot and a failure reason. CHECKs: `running` ⇔ no `completed_at`; counters ≥ 0. |
| `ingestion_issues` | Per-URL problems of a run (capped at 10,000 stored; the counters stay exact) |
| `pages` | Stable identity: UUID, unique `canonical_url`, partial-unique `external_id`, and a summary of the current version. The `(id, current_version)` FK to `page_versions` is **DEFERRABLE INITIALLY DEFERRED**. |
| `page_versions` | **Immutable** content history: hash + `hash_version`, provenance (run, source, URLs, redirect chain, status), canonical and status, metadata, headings, body HTML/text, link counts |
| `existing_links` | **Immutable** body links of one version, with a composite FK to `(page_id, version)` |
| `known_urls` | Every URL observed: the last page it led to and its last outcome (page, redirect, 404, 410, robots, error…) |

`ken_reject_mutation()` plus row triggers reject `UPDATE`/`DELETE` on `page_versions` and
`existing_links`. No vector extension or column was added; that is Phase 2.

## 3. Ingestion sources

| Source | Command | Status |
|---|---|---|
| `PublicWebContentSource` | `make crawl` (refuses, exit 2, until `KEN_CRAWL_BASE_URL` is set) | Complete. Verified live against the fixture site on 127.0.0.1: 35 requests, all GET; robots.txt fetched once; the cookie the server set was never sent back; no disallowed, nofollow or `utm_` URL requested; 18 pages; a re-crawl created **0** new versions. |
| `FileExportContentSource` | `make ingest-file FILE=…` | Complete. `valid.jsonl` gave 4 pages; then `v2_changed.jsonl` gave exactly 1 new version and 3 unchanged. |
| `CMSContentSource` (mock reader) | `make ingest-cms-mock` | Adapter complete; mock reader only |

## 4. API endpoints added (all `GET`, all returning `ListResponse[T]` / `ErrorResponse`)

```
/inventory/pages                                  ?limit&offset&q&publication_state
/inventory/pages/{page_id}
/inventory/pages/{page_id}/versions               ?limit&offset
/inventory/pages/{page_id}/versions/{version}
/inventory/pages/{page_id}/links                  ?limit&offset&version&link_type
/inventory/links                                  ?limit&offset&link_type&resolution&target_page_id
/inventory/runs                                   ?limit&offset&source_type&status
/inventory/runs/{run_id}
/inventory/runs/{run_id}/issues                   ?limit&offset&severity
```

Errors use stable codes: 404 is `page_not_found` / `not_found` and never echoes the id; 422 is
`validation_failed`. Mutation methods return 405. A snapshot test asserts that the inventory API is
GET-only and that every 4xx is an `ErrorResponse`.

## 5. Frontend screens added

`/inventory` (search, filter, pagination, empty/error states) · `/inventory/pages/[pageId]` (identity,
current version, headings, metadata, text, version history with `?version=N`, links with
resolution badges, inbound links, real 404) · `/inventory/runs` · `/inventory/runs/[runId]` (counters,
config snapshot, issue counts, issues). The navigation gains Inventory and Runs; `/system` is
unchanged. Electron readiness is intact: no Electron import (verified, since a probe file importing
`electron` fails lint), the portability guard applies to every new `lib/` file, and data comes only
via FastAPI.

## 6. Dependencies added

| Package | Version | Licence | Why |
|---|---|---|---|
| `beautifulsoup4` | 4.15.0 | MIT | HTML parsing (approved in TRD §2), used with Python's built-in `html.parser` only: no lxml or native code |
| `soupsieve` | 2.9.2 | MIT | Required by beautifulsoup4. Pinned below the 3.0 line released 3 days earlier, per the soak policy |

No frontend dependency changes. No Electron. No new infrastructure.

## 7. Test and verification results (final run, on the final code)

| Gate | Result |
|---|---|
| `make verify` | **exit 0** — "verify: all gates passed" |
| Ruff check / format | clean / 117 files formatted |
| mypy (strict on `ken_contracts`, `ken_core`, `ken_services`) | no issues in 116 files |
| Python tests | **946 passed**, 0 failed, 0 skipped (gate run: 847 unit and contract + 99 integration) |
| Contract tests | 110, including the OpenAPI snapshot, GET-only and error-envelope checks |
| Frontend lint / typecheck / tests | clean / clean / **468 passed** (13 files; 182 new) |
| Docker Compose validation | all three modes OK |
| `make doctor` | 0 FAIL, 1 WARN (embedding model not pulled; expected until Phase 2) |
| Alembic up / down / up | OK on the test DB; dev DB at `0002_content_inventory` |
| OpenAPI drift | live app == `contracts/openapi/openapi.json`; `types.gen.ts` regenerated |
| Electron guard | a probe component importing `electron` fails ESLint with the ADR-0008 message |
| Live end to end | Crawl, imports, mock CMS → dev DB → API (all 9 routes 200; 404/422/405 correct) → web (all 4 screens 200, unknown page 404) |

Phase 1 test inventory:

| Area | Tests |
|---|---|
| URL normalisation and scope | 45 |
| HTML discovery | 14 |
| Normalisation | 100 |
| Hashing | 10 |
| robots.txt | 34 |
| Crawler, MockTransport | 47 |
| Crawler, local sockets | 5 |
| File export | 28 |
| CMS source | 21 |
| Spec-derived fixture corpus | 57 |
| QA hardening | 14 |
| API | 58 |
| CLI | 17 |
| Ingestion (unit) | 13 |
| Schema / triggers / migration | 23 |
| Ingestion (DB) | 27 |
| Reader / link resolution | 29 |
| End to end, crawl and export → DB → reader | 5 |

These cover every area the brief listed: URL normalisation, canonical handling, redirects,
duplicates, hashing, version creation, unchanged pages, internal link extraction, external
exclusion, malformed HTML, missing title/canonical, loops, robots, domain restriction, file import,
run accounting, DB persistence, API contracts and frontend pages.

The fixture corpus was written **from the spec**, independently of the implementation. It found
**no** mismatches, and deliberately corrupting two expected results made the corpus tests fail, so
they are not vacuous.

## 8. Architecture review

**Before fan-out: APPROVED WITH CHANGES.** All 8 required changes were made before any
implementation:
- canonical precedence (no identity flip-flop);
- `Content-Language` carried;
- `page_type` and dates;
- `known_urls` implication CHECK;
- frozen event subject constants;
- `page.deleted` on transition only;
- advisory lock plus `FOR UPDATE`;
- docs.

**Final: APPROVED WITH CHANGES.** It verified each pre-fan-out change in code (with file:line), and
confirmed:
- inward dependencies;
- thin routes;
- no LLM;
- no Phase 2 work;
- no mutation-capable CMS object.

Its required changes were documentation only, and all were applied:
- an ADR-0009 `Content-Language` correction;
- the `PageInfo` note in CONTRACTS;
- the stale Phase 0 text in ARCHITECTURE_OVERVIEW, DEVELOPMENT and `.env.example`;
- an ADR-0006 status note.

## 9. QA / security review

**Verdict: CONFIRMED — no blockers.** Every finding below was fixed in Phase 1 with a regression
test:

| Ref | Finding | Fix |
|---|---|---|
| M1 | Hostile HTML (nested anchors; 20k links in one block) made normalisation quadratic: 3.6 s for 60 KB, 21.5 s for 2.5 MB | Per-anchor scan bounded, block hash computed once, 10,000 links per page with a truncation flag. Now 0.6 s for the 2.5 MB case. |
| M2 | A NUL character made a page fail to persist on every run; `?q=%00` returned 500 | NUL replaced with U+FFFD before parsing; `q` rejects NUL with 422 |
| M3 | robots.txt matcher was O(pattern × path) on the event loop | Linear split-and-`find` matcher (checked against a regex oracle on 10,000 random cases); pattern-length and rule-count caps |
| L1 | Redirect hops not counted in `max_pages` | Intended; now stated exactly in the contract and ADR: a chain is one request, bounded by `max_redirects` |
| L2 | gzip responses inflated per chunk before the size cap (148 MB peak) | `Accept-Encoding: identity` |
| L3 | A duplicate export line rewrote its URL as a self-redirect | Self-duplicates are no longer treated as redirects |
| L4 | robots.txt 429 treated as allow-all | Treated like a 5xx: disallow |
| L5 | Web fixtures used a real Ken domain | Replaced with `example.com` |
| L6 | A probe test hard-coded the test DB name | Derived from `KEN_TEST_DATABASE_URL` |
| L7 | Stale docstring | Corrected |

Its remaining later-phase observations are in [NEXT_STEPS.md](NEXT_STEPS.md):
- the SIGKILL'd-run reaper;
- the lock lost on a dropped connection;
- the alias cycle;
- the httpx URL logging.

## 10. Architecture impact and contract changes

- **New ADR-0009.** ADR-0005 is amended (heartbeat persistence); ADR-0006 gets a status note.
- **New frozen module** `ken_contracts/inventory.py`, documented in CONTRACTS §13.
- **Contract base change:** always-present response fields are now `required` in OpenAPI
  (`json_schema_serialization_defaults_required`). The wire format is unchanged.
- **`PageInfo` round-trips:** an incoming `has_more` is accepted and dropped.
- **No new event names.** `crawl.completed` covers every source type.
- **New package `ken_services`** (reserved in ADR-0001). The dependency direction is
  `ken_api → ken_services → ken_core → ken_contracts`.
- **Phase 0 items closed:**
  - L3: mock call log bounded.
  - L4: heartbeat events only on the first tick and on status change.
  - L10: default credentials refused outside dev; test settings isolated from `KEN_*`.
  - Serialization-mode schemas.

## 11. Known gaps

See [NEXT_STEPS.md](NEXT_STEPS.md) for the full list. The main ones:
- **No real site, CMS or export has been ingested.** That needs your input (below).
- **Hard-killed runs:** a run killed with SIGKILL stays `running`, and losing the DB connection
  mid-run releases the single-writer lock early.
- **Moved pages:** a moved page becomes a new page, and history isn't merged.
- **No URL unification:** http/https and `www.` variants are never unified.
- **Recreated CMS pages:** a CMS page recreated under a new id at the same URL needs an operator to
  resolve it.
- **Read-only by code, not by role:** the inventory API uses the app's write-capable DB role.
  RBAC and least-privilege come in Phase 5.
- **Unauthenticated:** all endpoints are unauthenticated. That is fine locally; a shared
  environment needs RBAC first.
- **Process slip during this phase, disclosed:**
  - A scratch test server I started could not bind port 8765, which was already in use. The live
    fixture crawl therefore sent two read-only GET requests (`/robots.txt` and `/`) to your own
    local "AI Employees Dashboard" app on 127.0.0.1:8765.
  - Nothing there was changed. The one stored page, two events and the run row were removed from
    the project dev DB straight away.
  - The crawl was re-run on an OS-assigned port, after checking that the listening process was
    mine.

## 12. Information needed from you for real production ingestion

1. **Which source is authoritative:** the public site, a CMS read API, or a CMS export.
2. **Public site:** the exact base URL and extra hosts, permission to crawl (**ASK**), and the
   politeness settings and crawl window.
3. **CMS API:** endpoint, pagination and field details, and **read-only least-privilege
   credentials** (**ASK**; never in source control).
4. **Export:** a sample file, so it can be mapped to the JSONL format.
5. **Scope:** in-scope sections, exclusions, and the list of **protected** pages.
6. **Language:** whether the site is multilingual.

Phase 2 (Content Intelligence) begins only on your explicit instruction.
