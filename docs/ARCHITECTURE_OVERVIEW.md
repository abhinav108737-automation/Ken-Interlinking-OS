# Architecture Overview

How the Ken Research Agentic Interlinking OS is put together, and what Phases 0–3 actually built.
The governing specification is TRD.md; this page maps it onto the repository.

## The target architecture (TRD §3)

```
UI → FastAPI Control API → LangGraph Supervisor → Specialist Agent Nodes / Deterministic Services
   → Local Model Provider → PostgreSQL/pgvector → CMS API → Production
   → Analytics Import → Learning/Evaluation
```

The single most important rule (TRD §24): probabilistic reasoning must always terminate at a **typed
deterministic boundary** before any irreversible action.

```
LLM → structured decision → policy → deterministic validation → approval/authorisation
    → deterministic execution → measurement → learning
```

## What Phase 0 built (the platform; see below for Phase 1)

```
 Next.js (apps/web) ──server-side fetch──▶ FastAPI (apps/api/ken_api)
                                             │  GET /health  /ready  /meta
                                             ▼
                              ken_core: config · logging · db · health probes
                                 │              │                 │
                       ModelProvider       CMSProvider       SQLAlchemy (asyncpg)
                       ├ OllamaProvider    └ MockCMSProvider        │
                       └ MockModelProvider                          ▼
                              │                          PostgreSQL 16 (container, :5433)
                              ▼                          pgvector (enabled by migration 0003)
                     host Ollama :11434                  table: events
                                             ▲
                         worker (agents/ken_agents) — tick loop: heartbeat log every tick;
                         event on first tick and on status change (ADR-0005 amendment)
```

| Layer | Package | Contents |
|---|---|---|
| Contracts | `ken_contracts` | Frozen Pydantic models + protocols. Imports nothing internal. Phase 1 adds `inventory.py`; Phase 2 adds `intelligence.py` and `jobs.py`. |
| Platform | `ken_core` | Settings, structured logging, DB engine/session, ORM models (`events` + the six inventory tables), repositories, readiness probes, provider implementations |
| Deterministic services | `ken_services` | `inventory/` (Phase 1): URL normalisation, HTML discovery, normaliser + content hash, robots.txt, the three content sources, ingestion, reader, CLI. `intelligence/` (Phase 2): taxonomy, analysis, protection, eligibility, relationships, embedding recipe, pipeline, reader, CLI. `jobs/` (Phase 2): the generic job runner and reader. No LLM in any of them. |
| Control API | `ken_api` | App factory, request-id middleware, error envelope, health/ready/meta, **read-only `/inventory/*`** |
| Agent runtime | `ken_agents` | Worker tick loop only. No LangGraph, no reasoning. |
| UI | `apps/web` | System status; **inventory, page detail and run screens** (read-only); typed platform bridge; environment-agnostic API client core |
| Desktop (future) | `apps/desktop` | README only — Electron deliberately not installed (ADR-0008) |

Dependencies point **inward** only:
`ken_api` → `ken_services` → `ken_core` → `ken_contracts`, and `ken_agents` → `ken_core`.

## What Phase 1 added: the content inventory

```
 ContentSourceProvider ──SourceDocument / SourceIssue──▶ normalize_document (pure, no LLM)
 ├ PublicWebContentSource  robots.txt · scope · rate limit        │ NormalizedPage + content_hash
 ├ FileExportContentSource JSONL                                  ▼
 └ CMSContentSource ── CMSInventoryReader (read-only; mock)   run_ingestion (advisory lock,
                                                               one transaction per document)
                                                                  │
          PostgreSQL: ingestion_runs · ingestion_issues · pages · page_versions* · existing_links*
                      · known_urls · events (page.created/updated/deleted, crawl.completed)
                                                                  │      * immutable (trigger)
          GET /inventory/* ◀── InventoryReader (SqlInventoryReader) ◀┘
                 ▲
          apps/web /inventory, /inventory/pages/[id], /inventory/runs, /inventory/runs/[id]
```

Runs start from the CLI only (`make ingest-file`, `make ingest-cms-mock`, `make crawl`); the API cannot
start one. Design and rules: [ADR-0009](adr/ADR-0009-content-inventory.md).

## What Phase 2 added: content intelligence

```
 page_versions (current) ──▶ analyze_page (pure; taxonomy policy v1)  ──▶ page_analyses*
                         ──▶ embedding_text ─▶ ModelProvider.embed(EMBEDDING) ─▶ page_embeddings* (pgvector)
                         ──▶ match_protections / ingestion CMS sync ─▶ page_protections
 fresh analyses + embeddings ──▶ taxonomy + top-k cosine edges ──▶ page_relationships (version-stamped)
 read time: compute_eligibility(live state) ─▶ LinkEligibility (never stored)
                                                                   * immutable (trigger)
 make analyze ─▶ job_runs (one per kind, advisory lock) ─▶ job.completed
 GET /intelligence/* · GET /jobs/runs*  ◀── SqlIntelligenceReader / SqlJobReader
 apps/web: /intelligence · /intelligence/topics/[id] · /intelligence/search · /jobs · page-detail panel
```

- The embedding dimension is the provider's: the column is dimension-less, and queries filter on
  the provider-reported (model, dimensions).
- Similarity is an exact cosine KNN; an ANN index is deferred until latency is measured.
- Design: [ADR-0010](adr/ADR-0010-content-intelligence.md).

## What Phase 3 added: the recommendation engine

```
 eligible source ──▶ channels: semantic (pgvector) · lexical (tsvector FTS) · taxonomy (edges) · graph (under-linked)
                 ──▶ pair filters: self · protected · experiment hook · eligibility · already linked · duplicate/live
                                   · previously rejected · page type/intent · excluded target paths
                 ──▶ score: relevance = Σ policy weight × component  (ranking_weights.v1)
                 ──▶ placement: a verbatim phrase in a safe text run (never headings/tables/quotes/code/links,
                                never the source's own market without the target's geography in context)
                 ──▶ total = relevance × placement_quality ──▶ caps: density · per source · open plans per target
                 ──▶ link_plans* (content immutable; status via ALLOWED_TRANSITIONS → link_plan_transitions*)
                     link_candidates* (audit of every candidate) · opportunities · link.proposed events
 GET /recommendations* ◀── SqlRecommendationReader      * immutable / append-only (DB triggers)
 apps/web: /recommendations · /recommendations/[id] · /recommendations/opportunities · /recommendations/candidates
```

- Recommendations are deterministic: no LLM is involved, and every number is checkable from the
  plan record.
- `plan.id` is the stable link id for Phase 6 deployment and Phase 7 analytics.
- Design: [ADR-0011](adr/ADR-0011-recommendation-engine.md).

## What Phase 4 added: the agent review workflow

A LangGraph `StateGraph` (ADR-0012) runs one supervised review per source page:
`load_page → triage → understand → discover → plan → critique → check_invariants →
policy_gate → finalize`.

- **One plan producer.** The deterministic engine is the only producer of plans. Agents review
  `proposed` plans and hand accepted / uncertain ones to `awaiting_approval` through ONE service
  call (`hand_off`), whose actors are bound inside the service.
- **Narrow tools.** Nodes get a `ToolBox` over the frozen `AgentBackend` protocol: five READ tools
  and one REVIEW tool. They never receive a session, credentials, a filesystem, a shell, a CMS
  client or an actor string. This is enforced by a static test.
- **Typed model slots.** The FAST planner and the REASONING critic are reached by role through
  `ModelProvider`. Their output is validated (`PlannerSelection`, `CriticAssessment`), and
  every model problem falls back to a recorded deterministic rule.
- **Audit.** `agent_runs` (frozen once finished), append-only `agent_steps` (per node: timing,
  model role and reported model, fallback, tool calls, bounded output, trace and span ids) and
  append-only `agent_run_plans` (one review per plan). Every run belongs to a `job_runs` batch.
- **No egress.** LangSmith/LangChain tracing variables are refused, and a test runs the whole
  graph with sockets blocked.

## What Phase 5 added: the reviewer control plane

- **Auth.** An `AuthProvider` abstraction, implemented by a local provider: users in
  PostgreSQL, stdlib `scrypt`, opaque revocable Bearer sessions. The API is Bearer-only; the
  web app keeps the token in an HttpOnly cookie and forwards it server-side, which keeps
  Electron portability (ADR-0008, option 1).
- **RBAC.** Deny by default: every route except `/health`, `/ready` and `POST /auth/login`
  needs a permission. Route-introspection and role-matrix tests enforce it.
- **Human review.** A deterministic `ReviewService` over the transition service. Its actor
  comes from the session. Actions are idempotent (`operation_id`), version-aware
  (`expected_status`, freshness) and re-validated under the plan lock with the engine's shared
  validity rules. Writers are serialised per source. Edits supersede the old plan.
- **Feedback and audit.** Append-only `review_actions`, plus `link.approved` /
  `link.rejected` / `link.proposed` events. These are the input to Phase 8 learning.
- **UI.** A dashboard, the inbox with bulk review, plan review with a before/after preview and
  a span picker, the audit log, policies, and role-aware controls.

## What Phase 6 added: controlled deployment

- **Deployment service.** One source page per deployment, up to 20 approved plans:
  validate → `deploy` permission → dry-run → durable `applying` intent → CMS apply, with the
  CMS page version as the optimistic lock → verify → typed `link.*` events → rollback. It
  holds a per-source session lock, and reconciliation resends stored requests.
- **Deterministic HTML mutation** (`ken_services.deployment`). A position map over the
  inventory's own block walk splices exactly one `<a>`, refuses whenever unsure, re-checks
  itself, and guarantees `remove(insert(x)) == x`. The verifier checks presence, href, anchor,
  block, hidden state, nesting and target reachability, with `inconclusive` for transient
  errors.
- **CMS providers.** `mock`; `simulated` (its own database plus a loopback site, so the full
  loop including re-crawl runs locally); `http` (TRD §13, gated: loopback-only unless allowed,
  writes off unless enabled).
- **Integrity.** A database trigger enforces plan status transitions, readers are READ ONLY,
  stuck job runs are reaped, and physically live links occupy their pair until rolled back.

## Boundaries that are enforced in code, not by convention

| Boundary | Where enforced |
|---|---|
| Models chosen by role, never by name | `ModelProvider` takes `ModelRole`; resolution inside the provider |
| LLM output validated before use | `complete_structured` returns `StructuredResult[T]` or raises `ModelSchemaViolationError` |
| Mutations idempotent + version-aware | `operation_id` and `expected_version` mandatory on `LinkChangeRequest` |
| Mutations reversible + attributed | `DeploymentResult.previous_version` mandatory; `rollback(actor=...)` mandatory |
| Unknown keys rejected | every contract model inherits `extra="forbid"`; a contract test checks each one |
| AUTO_APPROVE disabled (TRD §20) | `Settings` refuses to construct if it is true (env, `.env` or constructor), so the process will not start; `Settings` is frozen, so it cannot be switched on at runtime either. *Limit:* `model_copy(update=...)` / `model_construct` skip validation — the Phase 9 policy engine must not take this flag from a `Settings` instance. |
| No credentials in responses or logs | `public_settings()` allowlist via `urlsplit`; `hide_input_in_errors`; `ErrorResponse` never carries exception text |
| Liveness cannot fail on dependencies | `/health` performs zero I/O; tested against a broken database |
| Migrations never touch the developer's own PostgreSQL | `migrations/env.py` refuses port 5432 on host-local addresses (`localhost`, all of `127.0.0.0/8`, `::1`, `0.0.0.0`/`::`, IPv4-mapped loopback, socket DSNs, the container-to-host gateway names). The target is resolved through the asyncpg dialect, so `?port=`/`?host=` overrides and multi-host URLs can't hide it. It does not resolve DNS, so exotic spellings (`127.1`, decimal/hex IPs) are not caught — it guards accidents. |
| Promotion is governed | `skill.promoted` / `policy.promoted` are rejected without a human `promoted_by` and an `evaluation_ref` — at construction, by a frozen envelope that refuses field assignment, **and again at persistence** (`append_event` re-validates, so `model_copy` or in-place payload edits cannot slip one into the log) (CONTRACTS rule 11) |
| UI never imports Electron or reaches the database | ESLint guards across `apps/web` + tests proving them; data only via FastAPI (ADR-0008) |
| Page history is immutable | a database trigger rejects `UPDATE`/`DELETE` on `page_versions` and `existing_links`; a deferred FK keeps `pages.current_version` pointing at a real version |
| A version only when content changes | `content_hash` over content fields only; equal hash ⇒ no row (tested end to end) |
| Crawler stays polite and on-site | no default site; robots.txt always honoured (no override); scope checked on every redirect hop; GET only, no cookies or credentials; rate, concurrency, size, depth and page caps |
| Inventory never holds a CMS mutation object | `CMSContentSource` takes a read-only `CMSInventoryReader`, never `CMSProvider` |
| Inventory API cannot mutate | GET-only routes, asserted by the OpenAPI snapshot test; `body_html` is never rendered by the UI |
| One ingestion run at a time | session-level PostgreSQL advisory lock |

`docs/CONTRACTS.md` has the full rule set.

## Approved stack

FastAPI · Pydantic · SQLAlchemy · Alembic · asyncpg · PostgreSQL · pgvector · Ollama · structlog ·
httpx · BeautifulSoup (`html.parser` backend, Phase 1) · Next.js · TypeScript · Tailwind CSS. LangGraph (Phase 4), Playwright (Phase 6) and others are
approved but deliberately deferred — see ADR-0005.

## Forbidden without an explicit user decision

These must not be added — not as a dependency, not as a compose service, not "temporarily":

- **Qdrant, Neo4j** — PostgreSQL + pgvector is the store (TRD §2)
- **Redis, Kafka, Celery, Temporal** — no queue or broker; the worker is a tick loop (ADR-0005)
- **Any paid model API** — OpenAI, Anthropic, Gemini, or any other; inference is local via Ollama
- **Managed vector databases, paid SaaS, paid Browser Use Cloud**
- **A second agent framework** stacked on LangGraph without an ADR (TRD §2)

Proposing one requires stopping and asking the user (CLAUDE.md), with an ADR.

## Decision records

| ADR | Decision |
|---|---|
| [0001](adr/ADR-0001.md) | Monorepo with one multi-root Python distribution |
| [0002](adr/ADR-0002.md) | Colima as the container runtime |
| [0003](adr/ADR-0003.md) | pgvector provisioned, not enabled, in Phase 0 (amended by ADR-0010) |
| [0004](adr/ADR-0004.md) | asyncpg as the single driver (Apache-2.0; replaced LGPL psycopg 3) |
| [0005](adr/ADR-0005.md) | Worker is a tick loop; heavy dependencies deferred |
| [0006](adr/ADR-0006.md) | Minimal Phase 0 schema: `events` only (extended by migration 0002, ADR-0009) |
| [0007](adr/ADR-0007.md) | Hybrid local topology; host Ollama preferred |
| [0008](adr/ADR-0008-electron-readiness.md) | Electron readiness: web UI canonical, typed bridge, Electron deferred |
| [0009](adr/ADR-0009-content-inventory.md) | Phase 1 content inventory: sources, identity, immutable versions, read-time link resolution, crawler politeness |
| [0011](adr/ADR-0011-recommendation-engine.md) | Phase 3 recommendation engine: four retrieval channels, ordered filters, transparent policy-weighted score, verbatim context-supported placement, immutable LinkPlans with an allowed-transition lifecycle |
| [0012](adr/ADR-0012-agent-orchestration.md) | Phase 4 agent orchestration: a LangGraph supervisor that reviews deterministic plans with optional, validated local-model slots, deterministic fallbacks, narrow tools and an atomic hand-off to the human queue |
| [0013](adr/ADR-0013-reviewer-control-plane.md) | Phase 5 reviewer control plane: local auth provider abstraction (scrypt, opaque Bearer sessions, HttpOnly cookie in the web tier), deny-by-default RBAC, idempotent version-aware human review with structured feedback, supersede-on-edit |
| [0014](adr/ADR-0014-controlled-deployment.md) | Phase 6 controlled deployment: two-phase durable intent with reconciliation, per-source lock, deterministic splice-only HTML mutation, HTTP/HTML verification (browser deferred), simulated CMS, gated production HTTP adapter, DB status-integrity trigger |
| [0010](adr/ADR-0010-content-intelligence.md) | Phase 2 content intelligence: pgvector required from 0003, taxonomy-policy analysis, provider-defined embeddings, version-stamped relationships, protections, read-time eligibility, generic job runs |
