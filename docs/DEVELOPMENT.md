# Developer Setup

Local development for the Ken Research Agentic Interlinking OS on macOS (Apple Silicon).
Everything runs locally; nothing calls a paid service.

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Python | **3.12** at `/opt/homebrew/bin/python3.12` | The system `python3` is 3.9 and **will not work**. Always use `.venv/bin/...`. |
| Node / npm | `^22.22.2 \|\| ^24.15.0 \|\| >=26` (developed on 26) | Floor set by jsdom 30 / vitest 5, stricter than `next@16`'s ≥ 20.9 |
| Colima + Docker CLI + Compose | Colima 0.10, Docker 29, Compose 5 | Free and open source (ADR-0002). Run `colima start` after a reboot. |
| Ollama | your existing host install | Listening on `127.0.0.1:11434`. Needed for real embeddings: `ollama pull nomic-embed-text` (Apache-2.0). Without it, `/ready` is degraded and analysis runs with `--no-embeddings`. |

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

Expected `/ready` in Mode A from Phase 2 on, with `nomic-embed-text` pulled:
- postgres `ok`;
- migrations `ok` (head is 0003);
- pgvector `ok` (the extension is enabled by migration 0003, and it is **required**);
- model_provider `ok`;
- cms `ok`.

The aggregate is **`ready`, HTTP 200**. Without the embedding model, model_provider is `degraded`
and the aggregate is `degraded` (still 200).

## Content intelligence (Phase 2)

```bash
make analyze                       # classify, embed and relate every page whose current version is stale
make analyze ARGS="--no-embeddings"   # deterministic analysis only (no model server needed)
make analyze ARGS="--force"        # recompute relationships/protections for every fresh page
```

- **Re-running is free:** pages whose current version already has a fresh analysis and embedding
  are skipped.
- **Classification comes from `policies/taxonomy/taxonomy.v1.yaml`,** a versioned policy file. v1 is
  an illustrative seed until Ken supplies its real taxonomy.
- **Read-only endpoints:**
  - `GET /intelligence/coverage`;
  - `/intelligence/taxonomy` and `/intelligence/taxonomy/{node_id}/pages`;
  - `/intelligence/pages/{page_id}`, plus its `/similar` and `/relationships`;
  - `/intelligence/search?q=` (503 `model_unavailable` when the embedding model is down);
  - `/jobs/runs` and `/jobs/runs/{run_id}`.
- **Screens:** `/intelligence`, `/intelligence/topics/[nodeId]`, `/intelligence/search`, `/jobs`, and
  an Intelligence panel on every page detail.
- **The similarity threshold is model-specific:** `KEN_SIMILARITY_MIN_SCORE=0.78` is calibrated for
  `nomic-embed-text`. With `KEN_MODEL_PROVIDER=mock`, use about `0.3`. Re-calibrate with
  `evals/intelligence/benchmark.py` after changing models.
- **Evals:** `.venv/bin/pytest tests/unit/intelligence_evals`. Benchmark:
  `.venv/bin/python evals/intelligence/benchmark.py --provider ollama`.

## Recommendation engine (Phase 3)

```bash
make recommend                         # propose links for every eligible source (idempotent)
make recommend ARGS="--source <id>"    # one or more specific source pages
```

- Run it after `make analyze`; it needs fresh analyses. Embeddings are optional, and the
  semantic channel switches off without them.
- **Policy is data:** `policies/recommendation/recommendation.v1.yaml` holds retrieval sizes,
  caps, density, intent and page-type compatibility, and anchor rules. Weights live in
  `policies/ranking/ranking_weights.v1.yaml`. Change them by adding a `v2` file, never by editing
  in place.
- **Read-only endpoints:** `GET /recommendations` (filters: status, source, target, minimum
  score), `/recommendations/{id}`, `/recommendations/candidates`,
  `/recommendations/opportunities` and `/recommendations/summary`. Approve, reject and edit come
  in Phase 5.

## Agent review workflow (Phase 4)

```bash
make agents                            # review every source with unreviewed proposed plans
make agents ARGS="--source <id> ..."   # a manual batch of specific source pages
```

- Run it after `make recommend`. It moves accepted and uncertain plans to `awaiting_approval`;
  critic-rejected plans stay `proposed`. A plan is reviewed by agents once.
- **Models are optional.** `policies/agents/agents.v1.yaml` turns the FAST planner (off) and the
  REASONING critic (on) on or off. With Ollama down, or the model off, the deterministic rule
  is used and recorded. `KEN_MODEL_PROVIDER=mock` gives a fully offline run. The local
  `qwen3:8b` critic takes about 10 s per source.
- **Tracing is refused.** An agent run will not start while any `LANGSMITH_*` or
  `LANGCHAIN_*` tracing variable, endpoint or key is set.
- **Read-only endpoints:** `GET /agents/runs` (filters: status, outcome, source page),
  `/agents/runs/{id}` and `/agents/plans/{plan_id}/reviews`. The screens are at `/agents`.
- **Evals:** `evals/agents/README.md` (recorded model outputs; the benchmark uses only a
  `*_test` database).
- **Screens:** `/recommendations`, the plan detail (highlighted placement, score breakdown,
  reasons, history), opportunities, candidates, and per-page sections on page detail.
- **Evals:** `KEN_REQUIRE_INTEGRATION_DB=1 .venv/bin/pytest tests/integration/test_recommendation_evals.py -rA`
  and `.venv/bin/python evals/recommendations/benchmark.py`. The benchmark refuses any database
  not named `*_test`.

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
| `KEN_REQUIRE_PGVECTOR` | `true` (from Phase 2) | missing pgvector makes `/ready` 503 |
| `KEN_TAXONOMY_PATH` | `policies/taxonomy/taxonomy.v1.yaml` | versioned taxonomy policy driving analysis |
| `KEN_EMBEDDING_DOCUMENT_PREFIX` / `_QUERY_PREFIX` / `_MAX_CHARS` / `_BATCH_SIZE` | `search_document: ` / `search_query: ` / `6000` / `16` | embedding recipe (prefixes are the default model's convention) |
| `KEN_SIMILARITY_TOP_K` / `KEN_SIMILARITY_MIN_SCORE` | `10` / `0.78` | `semantic_similar` edges; the threshold is model-specific |
| `KEN_RELATIONSHIP_MAX_PER_TYPE` | `25` | cap on stored taxonomy edges per (source page, relationship type) |
| `KEN_RECOMMENDATION_POLICY_PATH` | `policies/recommendation/recommendation.v1.yaml` | versioned recommendation policy (ADR-0011) |
| `KEN_AGENTS_POLICY_PATH` | `policies/agents/agents.v1.yaml` | versioned agent supervisor policy (ADR-0012); cross-checked against the recommendation policy |
| `KEN_REQUIRE_OLLAMA` | `false` | true ⇒ missing model server makes `/ready` 503 |
| `KEN_AUTO_APPROVE_ENABLED` | `false` | **Must stay false.** The process refuses to start otherwise (TRD §20). |
| `KEN_CRAWL_BASE_URL` | *unset* | The one site the crawler may visit. **No default, by design.** |
| `KEN_CRAWL_ALLOWED_HOSTS` | empty | Extra in-scope hosts, comma-separated (subdomains are not implied) |
| `KEN_CRAWL_DELAY_S` / `_CONCURRENCY` / `_MAX_PAGES` / `_MAX_DEPTH` | `1.0` / `2` / `500` / `10` | Politeness and limits; robots.txt cannot be disabled. Also `KEN_CRAWL_TIMEOUT_S`, `_MAX_BYTES`, `_MAX_REDIRECTS`, `KEN_INGEST_MAX_FILE_BYTES` — see `.env.example` |

## A PostgreSQL without pgvector

From Phase 2, pgvector is required: migration 0003 creates the extension and the first vector column
(ADR-0010). A server without it, such as a plain Homebrew PG 16, can still apply migrations **up to
0002**:

```bash
.venv/bin/alembic upgrade 0002_content_inventory
```

0003 then fails with pgvector's own error, and the current build reports `/ready` **503**, because
the migrations are behind head. Always pass Alembic targets as `-x dburl=...`: two shell words, or
one quoted word. An unknown or empty `-x` key is refused rather than falling back to
`KEN_DATABASE_URL`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `SyntaxError` or `requires a different Python` | You ran system `python3` (3.9). Use `.venv/bin/...` or `make`. |
| `docker: unknown command: docker compose` | Compose plugin not registered: add `"cliPluginsExtraDirs": ["/opt/homebrew/lib/docker/cli-plugins"]` to `~/.docker/config.json`. |
| `Cannot connect to the Docker daemon` | `colima start` |
| `make test-all` fails with `KEN_REQUIRE_INTEGRATION_DB` | Postgres not running on :5433 — `make db-up`. (A bare `pytest` run auto-skips instead; the gate never does.) |
| `/ready` shows model_provider `unavailable` | Host Ollama is not running, or the embedding model is not pulled. `/ready` still returns 200 (degraded); `make analyze ARGS=--no-embeddings` still works. |
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
