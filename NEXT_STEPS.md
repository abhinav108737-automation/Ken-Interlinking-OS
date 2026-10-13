# Next Steps

**Current state:** Phase 0 (foundation) complete, plus a post-Phase-0 cleanup pass (asyncpg driver,
`skill.promoted`, Electron readiness, frontend portability) — see [PHASE_0_REPORT.md](PHASE_0_REPORT.md).
**Next phase:** Phase 1 — Content Inventory. **Not started.** It begins only on explicit user instruction.

## Decisions needed from you before or during Phase 1

| # | Decision | Why it matters | Where |
|---|---|---|---|
| 1 | **Real CMS access model** | Phase 1 crawls pages. Is the source the public site over HTTP, a CMS read API, or an export? Read-only credentials, if any, must be least-privilege (TRD §23) and are an ASK. | TRD §13, §23 |
| 2 | **Crawl scope and politeness** | Which hosts/paths, rate limits, robots.txt policy, and whether Phase 1 may touch the live site at all or should start from a fixture snapshot. | PRD §7.1 |

### Resolved in the post-Phase-0 cleanup (2026-10-13)

| Was | Resolution |
|---|---|
| Accept LGPL psycopg 3? | **No — replaced by asyncpg 0.31.0 (Apache-2.0)** for possible Electron distribution. No GPL/LGPL remains in the Python tree. [ADR-0004](docs/adr/ADR-0004.md) |
| Add `skill.promoted`? | **Yes — added**, with a governed `PromotionAuditPayload` (human `promoted_by`, `evaluation_ref`) enforced for both `skill.promoted` and `policy.promoted`. Rule for future event names: an explicit, recorded decision (CONTRACTS rule 12) — this also settles QA L11. |

## Phase 1 — Content Inventory (from IMPLEMENTATION_PLAN.md)

Crawler, parser, canonical page model, content hashing, existing-link extraction, page/version persistence.

Concretely, building on what Phase 0 froze:

- **Schema (migration `0002`)**: `pages`, `page_versions`, `existing_links` — designed from the real crawler's
  needs, which is why Phase 0 did not guess them (ADR-0006). Content hashes and canonical URLs are
  deterministic services (TRD §11), not LLM calls.
- **Package `ken_services`** (`services/ken_services`, reserved in ADR-0001): URL normalisation, content
  hashing, canonical validation, link extraction — all deterministic and unit-tested.
- **Dependencies**: `beautifulsoup4` (approved in TRD §2, deferred to Phase 1 in ADR-0005). `scrapy` only if
  scale demonstrably warrants it.
- **Events**: start emitting `page.created`, `page.updated`, `page.deleted`, `crawl.completed` — already
  frozen in `EventName`.
- **Worker**: the Phase 0 tick loop becomes the crawl scheduler's host. It must stay free of an in-process
  job queue (ADR-0005); any work queue is a deliberate PostgreSQL-backed design.
- **API**: first non-health endpoints. They must return the frozen `ErrorResponse` / `ListResponse[T]`
  envelopes, and each needs tests (CLAUDE.md).

## Carried forward from Phase 0 (known limitations, not blockers)

| Item | Target phase |
|---|---|
| `CREATE EXTENSION vector` + first vector column; read embedding dimension from `ModelProvider.embed()` (capabilities reports `None` until a real embed call) | 2 |
| Pull an embedding model (e.g. `nomic-embed-text`); until then `/ready` reports `model_provider` degraded — expected | 2 |
| OpenAPI response schemas are exported in validation mode, so always-present fields with defaults (`DependencyCheck.detail`, `MetaResponse.settings`, `ServiceIdentity.git_sha`) appear optional in TypeScript. Switch responses to serialization-mode schemas; regenerate `types.gen.ts`. The frontend already tolerates both. | 1 |
| Apply the identifier aliases (`PageId`, `DeploymentId`) to the remaining plain-`str` fields in `cms.py` (no wire change) | 1 |
| Freeze `TaskStatus` (incl. `AWAITING_APPROVAL`, TRD §16) and the autonomy decision enum (`BLOCK` / `REQUIRE_APPROVAL` / `AUTO_APPROVE`, TRD §20) | 3–4 |
| Extend mypy `strict` to `ken_agents` before LLM-boundary code lands there | before 4 |
| Starlette deprecation warning: `starlette.testclient` with `httpx` | when upgrading Starlette |
| CI workflow is authored but has never run (no git remote; pushing is ASK) | when a remote exists |
| Non-editable wheel build across four package roots not yet verified | before any artifact is distributed |
| `GET /meta` and `/docs` are unauthenticated — fine for local; RBAC (TRD §23) required before any shared environment | 5 |
| Colima does not start at login; run `colima start` after reboot | — |

## Carried forward from the post-Phase-0 cleanup

| Item | When |
|---|---|
| Bump `asyncpg` 0.31.0 → 0.32.x once it has a few weeks of soak; record in ADR-0004 | when convenient |
| **Desktop data-fetching decision:** `/system` is server-rendered. A desktop build must run the Next.js standalone server as a local sidecar, or fetch client-side with `createApiClient` (needs FastAPI CORS). Record in a follow-up ADR. | when Electron is scheduled |
| Re-verify the licence inventory of the **packaged desktop bundle** — `sharp` (LGPL) is excluded from the web runtime image by Next's trace, but desktop packagers copy `node_modules` differently | before any desktop distribution |
| Electron lint guards block accidents, not determined misuse (`require(name)`, `globalThis.require`, `eslint-disable`); keep code review on `apps/web` imports | ongoing |
| `platform` is resolved at module load: never branch rendered markup on it during SSR (hydration mismatch); call `getPlatform()` in effects | ongoing |
| The desktop-bridge global `window.kenDesktop` and its one-channel-per-method rule are fixed by ADR-0008; changing them is a contract change | Electron phase |
| **Version-aware event reads** (cleanup QA M1): `list_events` re-validates every stored row against *today's* rules, so tightening `PromotionAuditPayload` or adding a required field later would make historical rows unreadable — and one bad row fails the whole page. Validate payloads per `event_version`, and let the read path skip-and-log (or return raw) a bad row instead of failing. | before Phase 8 emits promotions |
| 5432 guard does not resolve DNS: `127.1`, decimal/hex IPv4, zone-scoped IPv6 and `localhost6` aren't caught (nobody types these by accident). If ever needed, classify via local `getaddrinfo`. | optional |
| Optionally also forbid `components/**` from importing a `SERVER_ENTRIES` module (`lib/api.ts`) by name; today the protection is that every other `lib/**` file is portability-guarded | optional |

## Independent QA review — items recorded for later phases

From the Phase 0 qa-security-reviewer pass. None blocks Phase 0; each must be closed by the phase shown.

| Ref | Item | By phase |
|---|---|---|
| L4 | Worker heartbeats go into the domain `events` table every 30 s (~2,880 rows/day/worker), with no retention, and Mode A and Mode B workers share `subject_id`. Decide retention or a separate table before real domain events arrive. | 1 |
| L1 | `ken_api/errors.py` maps every unlisted 4xx to `validation_failed` (e.g. 405). Map 401/403 → `permission_denied` and add a code for 405. | before auth (5) |
| L6 | Mock CMS accepts links **to** protected pages, `href` not matching `target_page_id`, `javascript:` hrefs and empty anchors. Phase 3 eligibility and the Phase 6 validator must reject these — **non-http(s) hrefs are a stored-XSS vector**. | 3 / 6 |
| — | Persist the CMS audit log to `events` (`link.deployed`, `link.rolled_back`); it is in-memory only in the mock. | 6 |
| L5 | Append-only `events` is not enforced in the database, and the app connects as superuser `ken`. Add an UPDATE/DELETE trigger or REVOKE, and a least-privilege role (TRD §23). | 5 / 6 |
| L9 | `/ready` is unauthenticated, opens 3 DB sessions per call (pool-exhaustion vector), and logs a traceback per failing probe during an outage. Gate behind RBAC; rate-limit or cache. | 5 |
| P1 | `Settings` is now frozen, but `model_copy(update=…)` / `model_construct` skip validation. The Phase 9 policy engine must read AUTO_APPROVE from a module-level `Final`, never from a `Settings` instance. | 9 |
| L2 | ~~`.dockerignore` matched secrets only at the context root~~ — **fixed in Phase 0** (`**/` patterns). | done |
| L3 | `MockModelProvider.calls` grows without bound in long-running Mode B processes (`health`/`capabilities` append on every `/ready` and tick). Bound it or record only when asked. | 1 |
| L7 | A second (no-op) rollback returns the *first* caller's `actor` in `RollbackResult`; the audit entry is correct. Clarify the contract semantics. | 6 |
| L8 | Floating image tags (`pgvector/pgvector:pg16`, `node:26-slim`, `python:3.12-slim`); `ollama/ollama:0.35.0` never pulled/verified; CI actions pinned by tag, not SHA; compose builds three identical images (api/migrate/worker). Pin by digest; build once. | before any shared env |
| L10 | `ken:ken` is the code default for `database_url` with no guard for `environment=production`; the migrate container logs plain text; `make_settings` in `tests/conftest.py` disables `.env` but not inherited `KEN_*` env vars. | 1 |

## Operating reminders

- Work one phase at a time; stop at the phase boundary (CLAUDE.md).
- Frozen contracts change only through the orchestrator (docs/CONTRACTS.md).
- Never add Qdrant, Neo4j, Redis, Kafka, Celery, Temporal or a paid API without an explicit user decision.
