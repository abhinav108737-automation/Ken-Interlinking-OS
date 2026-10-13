# Next Steps

**Current state:** Phase 1 — Content Inventory is **complete**. See [PHASE_1_REPORT.md](PHASE_1_REPORT.md).
Phases 0 and 0.5 (cleanup) are described in [PHASE_0_REPORT.md](PHASE_0_REPORT.md).

**Next phase:** Phase 2 — Content Intelligence. **Not started.** It begins only on explicit user
instruction.

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

## Phase 2 — Content Intelligence (from IMPLEMENTATION_PLAN.md)

Taxonomy, entity extraction, embeddings, pgvector and graph relationships, built on `page_versions`:

- Migration 0003: `CREATE EXTENSION vector` together with the first vector column (ADR-0003), with
  the dimension read from `ModelProvider.embed()`, never hard-coded.
- Pull a local embedding model (e.g. `nomic-embed-text`). Until then `/ready` reports
  `model_provider` degraded, which is expected.
- Classification and entities go through `complete_structured` (Pydantic-validated), keyed to a
  page **version**, so results are reproducible and re-run only when content changes.
- `known_urls` plus `existing_links` already provide the link graph to extend into `graph_edges`.

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
| The 422 for a malformed UUID echoes one offending character (pydantic's message; Phase 0 handler) | before auth (5) |
| No automated test enforces the inward dependency direction; the API → services edge is a by-name import | optional AST import test |
| **Desktop data-fetching decision** (ADR-0008) now covers the inventory screens too — they are server-rendered like `/system` | when Electron is scheduled |

## Carried forward from Phase 0 / 0.5

| Item | Target |
|---|---|
| Apply the identifier aliases (`PageId`, `DeploymentId`) to the remaining plain-`str` fields in `cms.py` (no wire change) | 3 |
| Freeze `TaskStatus` (incl. `AWAITING_APPROVAL`, TRD §16) and the autonomy decision enum (BLOCK / REQUIRE_APPROVAL / AUTO_APPROVE) | 3–4 |
| Extend mypy `strict` to `ken_agents` before LLM-boundary code lands there | before 4 |
| **Version-aware event reads** (cleanup QA M1): `list_events` re-validates stored rows against today's rules | before 8 |
| Bump `asyncpg` 0.31.0 → 0.32.x after soak; record in ADR-0004 | when convenient |
| Starlette deprecation warning: `starlette.testclient` with `httpx` | when upgrading Starlette |
| CI workflow authored but never run (no git remote; pushing is ASK) | when a remote exists |
| Non-editable wheel build across the package roots (now five) not yet verified | before any distribution |
| `GET /meta`, `/docs`, `/ready` and `/inventory/*` are unauthenticated: fine locally; RBAC (TRD §23) before any shared environment | 5 |
| QA L1: unlisted 4xx map to `validation_failed` (e.g. 405); map 401/403 → `permission_denied`, add a 405 code | before auth (5) |
| QA L5: append-only `events` not enforced in the DB (unlike `page_versions`/`existing_links`, which now are); app connects as superuser `ken` | 5 / 6 |
| QA L6: links **to** protected pages, `javascript:` hrefs and empty anchors must be rejected by Phase 3 eligibility / the Phase 6 validator | 3 / 6 |
| QA L7: a second (no-op) rollback returns the first caller's `actor` | 6 |
| QA L8: floating image tags; CI actions pinned by tag; three identical compose images | before any shared env |
| QA L9: `/ready` unauthenticated, 3 DB sessions per call | 5 |
| QA P1: the Phase 9 policy engine must read AUTO_APPROVE from a module-level `Final`, never a `Settings` instance | 9 |
| Persist the CMS audit log to `events` (`link.deployed`, `link.rolled_back`) | 6 |
| Re-verify the licence inventory of any packaged desktop bundle (`sharp` is LGPL) | before desktop distribution |
| 5432 guard does not resolve DNS (`127.1`, decimal IPs) | optional |
| Colima does not start at login: run `colima start` after a reboot | — |

**Closed in Phase 1:**
- QA L3: the mock provider's call log is bounded.
- QA L4: heartbeats are persisted only on the first tick and on status change (ADR-0005 amendment).
- QA L10: default `ken:ken` credentials are refused in staging/production, and `make_settings`
  ignores inherited `KEN_*` variables.
- Serialization-mode OpenAPI schemas: always-present fields are now required in TypeScript.

## Operating reminders

- Work one phase at a time; stop at the phase boundary (CLAUDE.md).
- Frozen contracts change only through the orchestrator (docs/CONTRACTS.md).
- Never add Qdrant, Neo4j, Redis, Kafka, Celery, Temporal or a paid API without an explicit user decision.
- Before crawling anything other than a local fixture, get the site approved. Before binding a test
  server, check that the port is free: 8765 on this machine is another local app.
