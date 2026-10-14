# Next Steps

**Current state:** Phase 3 (Recommendation Engine) is **complete**; see
[PHASE_3_REPORT.md](PHASE_3_REPORT.md). The earlier phases are covered in
[PHASE_2_REPORT.md](PHASE_2_REPORT.md), [PHASE_1_REPORT.md](PHASE_1_REPORT.md) and
[PHASE_0_REPORT.md](PHASE_0_REPORT.md).

**Execution mode:** the user has authorised autonomous, sequential execution of Phases 3–9, an
end-to-end local demo and a final report. Each phase has internal gates and a local commit plus
tag. Execution stops only for ASK/BLOCK actions.

**Next phase:** Phase 4 (LangGraph agent orchestration), proceeding under that authorisation.

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
| 8 | **PRD v1.1 §17.1 wording:** "a genuine native degraded mode" now holds only for schema ≤ 0002 (pgvector is required from Phase 2, ADR-0010) | A product-document revision, for your approval | decision |

## Hard gates carried into later phases (from the Phase 3 QA review)

| Gate | Phase |
|---|---|
| **Authenticated actors.** The transition service enforces actor CLASS authority (`TRANSITION_AUTHORITY`: only `user:` approves/rejects/supersedes), but trusts its caller about who the user is. Every mutating endpoint must derive `user:<id>` from the authenticated session, never from the request body. | 5 |
| **Deploy-time re-validation.** Before any CMS mutation, re-check freshness (`check_plan_freshness`), active protections and live eligibility of source and target, and the existing-link state. The recommendation run already expires invalid open plans, but a deploy may happen between runs. | 6 |
| **DB-level status integrity.** A direct `UPDATE link_plans SET status = …` by the app role is possible (the content trigger only guards content). Route status changes through a SECURITY DEFINER function or a transition-validating trigger, and use a least-privilege role. | 6 |
| Inline `style="display:none"` is not detected by placement (the `hidden` / `aria-hidden` attributes are). Post-publish verification must render the page. | 6 |

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
| The intelligence reader asks the provider for its identity on each request: fast normally, but a hung daemon adds the provider timeout. Add a short TTL cache and a request deadline. | 5 |
| `get_coverage` aggregates in Python, O(pages) per request (~300 ms at 1k pages). Aggregate in SQL or cache. | at scale |
| `page_protections` "never deleted" is enforced by the application. Add a trigger rejecting DELETE and non-deactivating UPDATEs. | 5 (with RBAC) |
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
| An ANN index; tuning channel weights on real labelled data | at scale / 8 |
| Starlette deprecation warning: `starlette.testclient` with `httpx` | when upgrading Starlette |
| CI workflow authored but never run (no git remote; pushing is ASK) | when a remote exists |
| Non-editable wheel build across the package roots (now five) not yet verified | before any distribution |
| `GET /meta`, `/docs`, `/ready` and `/inventory/*` are unauthenticated: fine locally; RBAC (TRD §23) before any shared environment | 5 |
| QA L1: unlisted 4xx map to `validation_failed` (e.g. 405); map 401/403 → `permission_denied`, add a 405 code | before auth (5) |
| QA L5: append-only `events` not enforced in the DB (unlike `page_versions`/`existing_links`, which now are); app connects as superuser `ken` | 5 / 6 |
| QA L6: `javascript:` hrefs and empty anchors must be rejected by the Phase 6 validator (links **to** protected pages are now rejected by Phase 3: `target_protected`, and open plans expire) | 6 |
| QA L7: a second (no-op) rollback returns the first caller's `actor` | 6 |
| QA L8: floating image tags; CI actions pinned by tag; three identical compose images | before any shared env |
| QA L9: `/ready` unauthenticated, 3 DB sessions per call | 5 |
| QA P1: the Phase 9 policy engine must read AUTO_APPROVE from a module-level `Final`, never a `Settings` instance | 9 |
| Persist the CMS audit log to `events` (`link.deployed`, `link.rolled_back`) | 6 |
| Re-verify the licence inventory of any packaged desktop bundle (`sharp` is LGPL) | before desktop distribution |
| 5432 guard does not resolve DNS (`127.1`, decimal IPs) | optional |
| Colima does not start at login: run `colima start` after a reboot | — |

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
