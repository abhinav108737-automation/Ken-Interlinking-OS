# Architecture Overview

How the Ken Research Agentic Interlinking OS is put together, and what Phase 0 actually built.
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

## What exists after Phase 0

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
                              ▼                          pgvector provisioned, not enabled
                     host Ollama :11434                  table: events
                                             ▲
                         worker (agents/ken_agents) — tick loop, heartbeat events
```

| Layer | Package | Phase 0 contents |
|---|---|---|
| Contracts | `ken_contracts` | Frozen Pydantic models + provider protocols. Imports nothing internal. |
| Platform | `ken_core` | Settings, structured logging, DB engine/session, `events` model, readiness probes, provider implementations |
| Control API | `ken_api` | App factory, request-id middleware, error envelope, health/ready/meta |
| Agent runtime | `ken_agents` | Worker tick loop only. No LangGraph, no reasoning. |
| UI | `apps/web` | Shell + system-status page; typed platform bridge; environment-agnostic API client core |
| Desktop (future) | `apps/desktop` | README only — Electron deliberately not installed (ADR-0008) |

Dependencies point **inward** only: `ken_api`, `ken_agents` → `ken_core` → `ken_contracts`.

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

`docs/CONTRACTS.md` has the full rule set.

## Approved stack

FastAPI · Pydantic · SQLAlchemy · Alembic · asyncpg · PostgreSQL · pgvector · Ollama · structlog ·
httpx · Next.js · TypeScript · Tailwind CSS. LangGraph (Phase 4), Playwright (Phase 6) and others are
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
| [0003](adr/ADR-0003.md) | pgvector provisioned, not enabled, in Phase 0 |
| [0004](adr/ADR-0004.md) | asyncpg as the single driver (Apache-2.0; replaced LGPL psycopg 3) |
| [0005](adr/ADR-0005.md) | Worker is a tick loop; heavy dependencies deferred |
| [0006](adr/ADR-0006.md) | Minimal Phase 0 schema: `events` only |
| [0007](adr/ADR-0007.md) | Hybrid local topology; host Ollama preferred |
| [0008](adr/ADR-0008-electron-readiness.md) | Electron readiness: web UI canonical, typed bridge, Electron deferred |
