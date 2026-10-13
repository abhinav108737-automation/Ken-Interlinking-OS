# Developer Setup

Local development for the Ken Research Agentic Interlinking OS on macOS (Apple Silicon).
Everything runs locally; nothing calls a paid service.

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Python | **3.12** at `/opt/homebrew/bin/python3.12` | The system `python3` is 3.9 and **will not work**. Always use `.venv/bin/...`. |
| Node / npm | `^22.22.2 \|\| ^24.15.0 \|\| >=26` (developed on 26) | Floor set by jsdom 30 / vitest 5, stricter than `next@16`'s ≥ 20.9 |
| Colima + Docker CLI + Compose | Colima 0.10, Docker 29, Compose 5 | Free and open source (ADR-0002). Run `colima start` after a reboot. |
| Ollama | your existing host install | Listening on `127.0.0.1:11434`. Optional in Phase 0. |

## First-time setup

```bash
colima start                # if not already running
make setup                  # .venv (3.12), pip install -e ".[dev]", npm ci, .env from .env.example
make dev                    # start Postgres on :5433 and apply migrations
```

## Daily loop — Mode A (preferred)

```bash
make dev                    # Postgres container + migrate
make dev-api                # terminal 1: FastAPI on http://127.0.0.1:8000
make dev-web                # terminal 2: Next.js  on http://127.0.0.1:3000
```

| Component | Where | Address |
|---|---|---|
| PostgreSQL | container | `127.0.0.1:5433` |
| Ollama | your host daemon | `127.0.0.1:11434` |
| FastAPI | native | `127.0.0.1:8000` |
| Next.js | native | `127.0.0.1:3000` |

Open <http://127.0.0.1:3000/system> to see live dependency status.

You never need to expose Ollama on `0.0.0.0` for this loop (ADR-0007).

### Port 5433, not 5432

This machine already runs its own PostgreSQL on **5432** with unrelated data. The project database is
the container on **5433**. If `make doctor` ever shows port 5432 as the database target, fix
`KEN_DATABASE_URL` before doing anything else.

## Endpoints

| Endpoint | Purpose | Notes |
|---|---|---|
| `GET /health` | Liveness | Zero I/O. Always 200 while the process is alive. |
| `GET /ready` | Readiness | 200 `ready`, 200 `degraded`, or 503 `not_ready`; same body shape for each |
| `GET /meta` | Build + non-secret settings | Never contains credentials |

Expected Phase 0 `/ready` in Mode A: postgres `ok`, migrations `ok`, pgvector `ok` (provisioned, not
enabled), model_provider `degraded` (the embedding model is intentionally not pulled), cms `ok` →
aggregate **`degraded`, HTTP 200**. That is the correct, healthy Phase 0 state.

## Quality gates

```bash
make test              # unit + contract (no DB, no network)
make test-integration  # needs Postgres on :5433; FAILS if unreachable
make test-all          # everything except live tests; FAILS if the test DB is unreachable
make test-live         # opt-in: real calls to the host Ollama (slow)
make web-test          # frontend vitest
make lint              # ruff + ruff format --check + eslint
make types             # mypy + tsc --noEmit
make compose-config    # validate compose in all three modes
make doctor            # environment matrix
make verify            # the whole Phase gate
```

## Mode B — optional full container stack

```bash
make compose-full-up        # postgres, migrate, api, worker, web
make compose-full-down
```

Mode B uses the deterministic mock model provider by default, because a container cannot reach the
`127.0.0.1`-bound host Ollama. For a real model in containers you need **both** `--profile
ollama-container` **and** `KEN_MODEL_PROVIDER=ollama` (`.env.compose` sets `mock`), plus a model pulled
into that container — slow on macOS, as the VM has no GPU. Alternatively see
`docker-compose.override.yml.example` and its security trade-off.

## Configuration

All settings use the `KEN_` prefix; see `.env.example` for every variable and its meaning.

| Variable | Phase 0 default | Meaning |
|---|---|---|
| `KEN_DATABASE_URL` | `postgresql+asyncpg://ken:ken@localhost:5433/ken_interlink` | Must use `postgresql+asyncpg://` (ADR-0004). Port **5432 on this machine is refused** for migrations. |
| `KEN_MODEL_PROVIDER` | `ollama` in `.env.example`, `mock` in code and tests | |
| `KEN_MODEL_FAST` / `_REASONING` / `_EMBEDDING` | `qwen3:8b-q4_K_M` / same / `nomic-embed-text` | Models are chosen by role, never named in code |
| `KEN_REQUIRE_PGVECTOR` | `false` | true ⇒ missing pgvector makes `/ready` 503 |
| `KEN_REQUIRE_OLLAMA` | `false` | true ⇒ missing model server makes `/ready` 503 |
| `KEN_AUTO_APPROVE_ENABLED` | `false` | **Must stay false.** The process refuses to start otherwise (TRD §20). |

## Native degraded mode (no pgvector)

The API also runs against a PostgreSQL **without** pgvector, such as a plain Homebrew PG 16: point
`KEN_DATABASE_URL` at it and keep `KEN_REQUIRE_PGVECTOR=false`. Migrations succeed, because migration
0001 creates no extension (ADR-0003), and `/ready` reports pgvector `unavailable` with HTTP 200.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `SyntaxError` or `requires a different Python` | You ran system `python3` (3.9). Use `.venv/bin/...` or `make`. |
| `docker: unknown command: docker compose` | Compose plugin not registered: add `"cliPluginsExtraDirs": ["/opt/homebrew/lib/docker/cli-plugins"]` to `~/.docker/config.json`. |
| `Cannot connect to the Docker daemon` | `colima start` |
| `make test-all` fails with `KEN_REQUIRE_INTEGRATION_DB` | Postgres not running on :5433 — `make db-up`. (A bare `pytest` run auto-skips instead; the gate never does.) |
| `/ready` shows model_provider `unavailable` | Host Ollama is not running. Optional in Phase 0; `/ready` still returns 200. |
| Startup fails mentioning `AUTO_APPROVE` | `KEN_AUTO_APPROVE_ENABLED=true` somewhere. It must be false in v1. |
| One query fails with `InvalidCachedStatementError` right after a migration | asyncpg caches prepared statements per pooled connection; a schema change invalidates them. It fails safe and the next query succeeds. Restart the API/worker after migrating to avoid it. |
| Requests stall when the database is unreachable | Connection attempts give up after `KEN_DATABASE_CONNECT_TIMEOUT_S` (default 5 s; asyncpg's own default is 60 s). |

## Electron readiness (desktop app is future work)

`apps/web` is the canonical UI. Electron is **not installed** and must not be added without an explicit
decision (ADR-0008). `make lint` enforces the boundary:

- any `electron` import in `apps/web` — static, `require`, or dynamic — is a lint **error**;
- reusable code (`components/**`, `lib/status.ts`, `lib/platform/**`, `lib/api-client.ts`) may not use
  Node built-ins, `process`, `server-only` or a database client;
- desktop-only capabilities go through `platform` from `@/lib/platform`, never a direct API.
- note: even a feature check like `typeof process !== "undefined"` is a lint error in reusable code — by
  design; put environment-specific code in a listed server entry (`lib/api.ts`) or under `app/`.

## Rules worth knowing before you change anything

- Contracts in `contracts/python/ken_contracts/` are **frozen**; see `docs/CONTRACTS.md`.
- Every schema change needs an Alembic migration; every new endpoint needs tests (CLAUDE.md).
- Never add Qdrant, Neo4j, Redis, Kafka, Celery, Temporal or a paid API without an explicit user decision
  (`docs/ARCHITECTURE_OVERVIEW.md`).
- Next.js telemetry is disabled (`NEXT_TELEMETRY_DISABLED=1`); keep it that way — it would send project
  data to an external service.
