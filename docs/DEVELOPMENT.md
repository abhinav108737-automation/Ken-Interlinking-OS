# Developer Setup

Local development for the Ken Research Agentic Interlinking OS on macOS (Apple Silicon).
Everything runs locally; nothing calls a paid service.

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Python | **3.12** at `/opt/homebrew/bin/python3.12` | The system `python3` is 3.9 and **will not work**. Always use `.venv/bin/...`. |
| Node / npm | `^22.22.2 \|\| ^24.15.0 \|\| >=26` (developed on 26) | Floor set by jsdom 30 / vitest 5, stricter than `next@16`'s ≥ 20.9 |
| Colima + Docker CLI + Compose | Colima 0.10, Docker 29, Compose 5 | Free and open source (ADR-0002). Run `colima start` after a reboot. |
| Ollama | your existing host install | Listening on `127.0.0.1:11434`. Optional in Phases 0–1. |

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

Open <http://127.0.0.1:3000/system> to see live dependency status, and
<http://127.0.0.1:3000/inventory> for the page inventory (Phase 1).

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

Phase 1 adds nine read-only `GET /inventory/*` endpoints: pages, page detail, versions, a single
version, a page's links, links across the site (filters: type, resolution, `target_page_id` for
inbound links), runs, run detail and run issues. Interactive docs are at
<http://127.0.0.1:8000/docs>. Nothing over HTTP can start a crawl or write anything (ADR-0009 §7).

## Content inventory (Phase 1)

Runs start from the command line only. Each run is recorded with its counters, a secret-free
configuration snapshot and per-URL issues; inspect them at `/inventory/runs`.

```bash
make ingest-file FILE=tests/fixtures/inventory/exports/valid.jsonl   # JSONL export (format: docs/CONTRACTS.md)
make ingest-cms-mock                                                  # the mock CMS seed pages (no real CMS)
make crawl                                                            # the ONE site in KEN_CRAWL_BASE_URL
```

- **The crawler has no default site.** `make crawl` refuses (exit 2) until `KEN_CRAWL_BASE_URL` is
  set. Only point it at a site you are allowed to crawl.
- **Politeness is not optional.** robots.txt is always honoured; requests are GET only, with no
  cookies or credentials; there is a 1 s gap between requests and at most 2 in flight. Limits:
  500 pages, depth 10, 15 s timeout, 5 MB per page (all `KEN_CRAWL_*`, see `.env.example`).
- **To try the crawler without any real site,** serve the fixture site
  (`tests/fixtures/inventory/site`) on `127.0.0.1` and set
  `KEN_CRAWL_BASE_URL=http://127.0.0.1:<port>/`. The tests do this in-process with
  `httpx.MockTransport`. Check that the port is really yours first: another local app on this
  machine already uses 8765.
- **Only one run can execute at a time** (a PostgreSQL advisory lock); a second run exits 3. Re-running
  unchanged content creates no new versions.
- **Exit codes:** 0 succeeded · 1 completed with errors or failed · 2 usage/config error · 3 another run
  is active.

Expected `/ready` in Mode A (Phases 0–1): postgres `ok`, migrations `ok`, pgvector `ok` (provisioned, not
enabled), model_provider `degraded` (the embedding model is intentionally not pulled), cms `ok` →
aggregate **`degraded`, HTTP 200**. That is the correct, healthy state until Phase 2 pulls an embedding model.

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

| Variable | Default | Meaning |
|---|---|---|
| `KEN_DATABASE_URL` | `postgresql+asyncpg://ken:ken@localhost:5433/ken_interlink` | Must use `postgresql+asyncpg://` (ADR-0004). Port **5432 on this machine is refused** for migrations. |
| `KEN_MODEL_PROVIDER` | `ollama` in `.env.example`, `mock` in code and tests | |
| `KEN_MODEL_FAST` / `_REASONING` / `_EMBEDDING` | `qwen3:8b-q4_K_M` / same / `nomic-embed-text` | Models are chosen by role, never named in code |
| `KEN_REQUIRE_PGVECTOR` | `false` | true ⇒ missing pgvector makes `/ready` 503 |
| `KEN_REQUIRE_OLLAMA` | `false` | true ⇒ missing model server makes `/ready` 503 |
| `KEN_AUTO_APPROVE_ENABLED` | `false` | **Must stay false.** The process refuses to start otherwise (TRD §20). |
| `KEN_CRAWL_BASE_URL` | *unset* | The one site the crawler may visit. **No default, by design.** |
| `KEN_CRAWL_ALLOWED_HOSTS` | empty | Extra in-scope hosts, comma-separated (subdomains are not implied) |
| `KEN_CRAWL_DELAY_S` / `_CONCURRENCY` / `_MAX_PAGES` / `_MAX_DEPTH` | `1.0` / `2` / `500` / `10` | Politeness and limits; robots.txt cannot be disabled. Also `KEN_CRAWL_TIMEOUT_S`, `_MAX_BYTES`, `_MAX_REDIRECTS`, `KEN_INGEST_MAX_FILE_BYTES` — see `.env.example` |

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
| `/ready` shows model_provider `unavailable` | Host Ollama is not running. Optional in Phases 0–1; `/ready` still returns 200. |
| Startup fails mentioning `AUTO_APPROVE` | `KEN_AUTO_APPROVE_ENABLED=true` somewhere. It must be false in v1. |
| One query fails with `InvalidCachedStatementError` right after a migration | asyncpg caches prepared statements per pooled connection; a schema change invalidates them. It fails safe and the next query succeeds. Restart the API/worker after migrating to avoid it. |
| Requests stall when the database is unreachable | Connection attempts give up after `KEN_DATABASE_CONNECT_TIMEOUT_S` (default 5 s; asyncpg's own default is 60 s). |

## Electron readiness (desktop app is future work)

`apps/web` is the canonical UI. Electron is **not installed** and must not be added without an explicit
decision (ADR-0008). `make lint` enforces the boundary:

- any `electron` import in `apps/web` — static, `require`, or dynamic — is a lint **error**;
- reusable code (`components/**` and all of `lib/**` except the server entry `lib/api.ts`) may not use
  Node built-ins, `process`, `server-only` or a database client;
- desktop-only capabilities go through `platform` from `@/lib/platform`, never a direct API.
- note: even a feature check like `typeof process !== "undefined"` is a lint error in reusable code — by
  design; put environment-specific code in a listed server entry (`lib/api.ts`) or under `app/`.

## Regenerating the API contract

After changing a route or a response model:

```bash
make openapi    # writes contracts/openapi/openapi.json and apps/web/lib/types.gen.ts
```

The snapshot test fails until the committed contract matches the code, so API drift is always a
deliberate, reviewed change.

## Rules worth knowing before you change anything

- Contracts in `contracts/python/ken_contracts/` are **frozen**; see `docs/CONTRACTS.md`.
- Every schema change needs an Alembic migration; every new endpoint needs tests (CLAUDE.md).
- Never add Qdrant, Neo4j, Redis, Kafka, Celery, Temporal or a paid API without an explicit user decision
  (`docs/ARCHITECTURE_OVERVIEW.md`).
- Next.js telemetry is disabled (`NEXT_TELEMETRY_DISABLED=1`); keep it that way — it would send project
  data to an external service.
