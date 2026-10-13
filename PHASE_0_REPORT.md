# Phase 0 Report — Foundation

| | |
|---|---|
| **Phase** | 0 — Foundation |
| **Date** | 2026-10-13 |
| **Status** | **Complete**, plus a post-Phase-0 cleanup pass (§10). All gates pass on the final code. |
| **Exit criterion** | "`docker compose up` starts the local stack and tests pass" — **met** (see §3) |
| **Phase 1** | **Not started.** Awaiting explicit instruction. |

---

## 1. What was built

| Area | Delivered |
|---|---|
| Monorepo | TRD §4 layout; one Python distribution across four package roots (ADR-0001); npm workspace for `apps/web` |
| Contracts (`ken_contracts`, frozen) | Health/readiness/meta, `ModelProvider`, `CMSProvider`, `AnalyticsProvider` (types only), event vocabulary + envelope, error and list envelopes, identifier conventions; committed `contracts/openapi/openapi.json` |
| Backend (`ken_api`, `ken_core`) | FastAPI app factory, `GET /health` (zero I/O), `/ready` (concurrent probes, 200/200/503), `/meta` (credential-free allowlist), request-id middleware, `ErrorResponse` handlers |
| Persistence | SQLAlchemy 2.1 async + **asyncpg** (psycopg 3 until the cleanup, §10), Alembic (async env), migration `0001_initial_events` — **`events` table only, no `CREATE EXTENSION`** (ADR-0003, ADR-0006); idempotent event repository |
| Model providers | `OllamaProvider` (host daemon, role-based model resolution, `think:false` + `<think>` stripping, typed structured output with retry), `MockModelProvider` (deterministic, 768-dim embeddings) |
| CMS | `MockCMSProvider`: idempotency, optimistic versioning (409 → `CMSVersionConflictError`), dry run, protected pages, rollback with actor + metadata, in-memory audit log. **No real CMS.** |
| Worker | `python -m ken_agents.worker`: tick loop, readiness probes, heartbeat event; no queue, no decisions (ADR-0005) |
| Frontend (`apps/web`) | Next.js 16.4 / React 19.3 / TS 5.9 strict / Tailwind 4; `/system` status page; typed server-side client generated from the frozen OpenAPI; telemetry disabled |
| Logging | structlog JSON/console, stdlib bridged, `request_id`/`trace_id`/`span_id` reserved |
| Infrastructure | `docker-compose.yml` (postgres default; `full` and `ollama-container` profiles), two Dockerfiles (non-root), `.env.example`, `.env.compose`, `Makefile`, `doctor.py`, CI workflow (authored) |
| Policy data | `policies/ranking/ranking_weights.v1.yaml` — TRD §12 weights as data, integrity-tested |
| Docs | README, DEVELOPMENT, ARCHITECTURE_OVERVIEW, CONTRACTS, ADR-0001…0007, evals/skills/tests READMEs, NEXT_STEPS |

**Deliberately not built** (Phase 0 scope): recommendation intelligence, crawler, embeddings, vector columns, LangGraph graph, publishing, learning, analytics, auth.

---

## 2. Validation — the 16-step checklist

| # | Check | Command | Result |
|---|---|---|---|
| 1 | Doctor | `make doctor` | **0 FAIL**, 1 WARN (embedding model not pulled — intended) |
| 2 | Backend unit (+ contract) | `pytest tests/unit tests/contract` | **240 + 54 passed** |
| 3 | Backend integration | `make test-integration` | **14 passed**, 0 skipped (strict DB gate on) |
| 4 | Ruff | `ruff check .` · `ruff format --check .` | clean · 76 files formatted |
| 5 | mypy | `mypy` (strict on contracts + core) | **75 files, no issues** |
| 6 | Frontend lint | `npm run lint -w apps/web` | clean (`--max-warnings=0`) |
| 7 | Frontend types | `npm run typecheck -w apps/web` | clean |
| 8 | Frontend tests | `npm run test -w apps/web` | **46 passed** |
| 9 | Compose validation | `make compose-config` | default / full / full+ollama-container all valid |
| 10 | Start the stack | Mode A and Mode B | both up (§3) |
| 11 | Endpoints | `curl /health /ready /meta` | 200 / 200 / 200 (§3) |
| 12 | Alembic up/down/up | integration test + manual native run | pass on container **and** on vector-less PG 16 (§3) |
| 13 | Independent QA | qa-security-reviewer | PASS WITH REQUIRED FIXES → re-review **CONFIRMED** (§5) |
| 14 | Fix Phase 0 blockers | — | 11/11 fixed and re-verified |
| 15 | This report | — | — |
| 16 | NEXT_STEPS.md | — | updated |

**Aggregate gate:** `make verify` → **exit 0**: 308 Python tests passed, 0 skipped, plus everything above. Live Ollama tier (opt-in): `make test-live` → **3 passed** against the host daemon.

### Doctor output (verbatim)

    Ken Interlink OS — doctor
    
    == Interpreter
      [OK  ] python               3.12.14 (/Users/abhinav/Documents/Projects/Ken-Interlinking OS/.venv/bin/python)
      [OK  ] virtualenv           /Users/abhinav/Documents/Projects/Ken-Interlinking OS/.venv
    
    == Packages
      [OK  ] import ken_contracts contracts/python/ken_contracts
      [OK  ] import ken_core      services/ken_core
      [OK  ] import ken_api       apps/api/ken_api
      [OK  ] import ken_agents    agents/ken_agents
    
    == Configuration & database
      [OK  ] settings             environment=local phase=0
      [OK  ] auto_approve         DISABLED (TRD §20)
      [INFO] database target      localhost:5433/ken_interlink
      [OK  ] postgres             reachable, server 16.15 (Debian 16.15-1.pgdg12+2)
      [OK  ] pgvector             available 0.8.7 (provisioned), not enabled (Phase 2)
      [OK  ] migrations           database at head (0001_initial_events)
    
    == Model server
      [OK  ] ollama               reachable at http://127.0.0.1:11434, provider=ollama
      [OK  ] model role fast      qwen3:8b-q4_K_M
      [OK  ] model role reasoning qwen3:8b-q4_K_M
      [WARN] model role embedding nomic-embed-text — not pulled — expected in Phase 0
    
    == Toolchain & containers
      [OK  ] node                 v26.11.1 (needs ^22.22.2 || ^24.15.0 || >=26)
      [OK  ] docker compose       5.6.0
      [OK  ] colima               running
      [INFO] compose services     postgres=Up 3 minutes (healthy)
    
    Summary: 0 FAIL, 1 WARN, 20 checks

---

## 3. Running-stack evidence

### Exit criterion

`docker compose --profile full up -d --build` starts postgres → migrate (exits 0) → api (healthy) + worker → web. **Note:** a bare `docker compose up` starts **only postgres** — by design, because the preferred daily loop runs the API and web natively (ADR-0007, your decision). The full stack is the `full` profile.

### Mode A — preferred (container Postgres :5433, host Ollama, native API + web)

- `/health` → 200. `/meta` → 200, `auto_approve_enabled: false`, database `localhost:5433/ken_interlink`, no credentials.
- `/ready` → **200 `degraded`**, exactly as planned:

  | check | required | status |
  |---|---|---|
  | postgres | yes | ok |
  | migrations | yes | ok (`0001_initial_events`) |
  | pgvector | no | ok — available 0.8.7, **not enabled** |
  | model_provider | no | degraded — fast/reasoning `qwen3:8b-q4_K_M` ok; embedding `nomic-embed-text` missing (not pulled, intended) |
  | cms | yes | ok (mock) |

- `http://127.0.0.1:3000/system` renders the live status; the API address never appears in the HTML; no `X-Powered-By`.
- `python -m ken_agents.worker --once` → exit 0; `worker.heartbeat` row written with a correlation id.

### Mode B — full container stack

`/ready` → **200 `ready`** (here `KEN_REQUIRE_PGVECTOR=true` and passes, because the image provisions pgvector). Web renders `Ready`; worker writes heartbeats; api/worker run as uid 10001, web as uid 1000; no `.env` in any image; installed packages match `requirements.lock.txt` exactly. App containers were then stopped; **postgres is left running** for Mode A.

### Native degraded mode (ADR-0003 guardrail)

Run on a **throwaway** PostgreSQL 16 cluster created from the Homebrew binaries, with its own data directory, on port 5434 — your server on 5432 was never connected to. `vector` available: **0**.

- `alembic upgrade → downgrade → upgrade`: table present → absent → present; only extension ever installed: `plpgsql`.
- API against it: `KEN_REQUIRE_PGVECTOR=false` → **200 `degraded`** (pgvector `unavailable`); `=true` → **503 `not_ready`, `Retry-After: 5`**.
- Cluster stopped and deleted afterwards. *This round-trip is manual / CI-only; the automatic guard is a static test* (see ADR-0003).

---

## 4. How the work was executed

| Step | Detail |
|---|---|
| W0 contract freeze | Orchestrator wrote contracts, config, logging, packaging; verified the multi-root editable install **before** any fan-out |
| W0b architecture review | architecture-reviewer: APPROVED WITH CHANGES → 13 required changes, then 3 blockers on re-review → all applied → cleared |
| Parallel fan-out | backend-engineer ×2 (W1+W2 DB/API; W4 mock CMS), agent-runtime-engineer (W3 model providers; then W6 worker), frontend-engineer (W5), eval-learning-engineer (W8) |
| Integration | Orchestrator re-ran every specialist's tests, lint and types, and checked ownership boundaries, before accepting any work |
| W10 QA | qa-security-reviewer: independent review → 11 required fixes → applied → sent back for re-verification |

**How specialists were invoked:** the six `.claude/agents/` definitions are not invocable as agent types in this session (confirmed — the harness rejected `architecture-reviewer`). As you authorised, each specialist ran as a `general-purpose` agent that first read its `.claude/agents/<role>.md` and followed it. No specialist edited a frozen contract.

---

## 5. Defects found and fixed during the phase

| Found by | Defect | Fix |
|---|---|---|
| Architecture review | `GET /meta` would publish `?password=` from a libpq-style DB URL | `urlsplit`-based allowlist + regression tests |
| Orchestrator (via test) | pydantic echoed the raw DB URL (with password) in startup errors | `hide_input_in_errors=True` |
| Architecture review | `ErrorResponse.of(**extra)` stringified arbitrary objects (exceptions can carry secrets) | explicit `ErrorDetail` only |
| Backend agent + orchestrator | structlog events **JSON-encoded twice** — no log field was queryable | single renderer via `wrap_for_formatter` |
| Orchestrator (live run) | every request logged twice (uvicorn re-enabled by our config) | uvicorn.access always silenced; middleware line kept |
| QA H1 | gate went green with **every integration test skipped** | `KEN_REQUIRE_INTEGRATION_DB` strict mode in `make verify`/CI |
| QA P1 | AUTO_APPROVE could be switched on at runtime by mutating `Settings` | `Settings` frozen |
| QA M8 | Ollama client honoured system/env proxies — prompts could leave the machine | `trust_env=False` |
| QA M7 | key=value DSN echoed in full (password included) on misconfiguration | scheme-only reporting |
| QA M2 | `/ready` returned 503 when launched outside the repo root | paths resolved from the repo root |
| QA M3 | Postgres healthcheck passed before first-time init finished (reproduced: 0.26 s early) | `pg_isready -h 127.0.0.1` |
| QA M4 | API image ignored the lockfile | `-c requirements.lock.txt` |
| QA M5 | nothing stopped migrations against your own PG on 5432 | guard in `migrations/env.py` + doctor FAIL |
| **Orchestrator's own regression** | first M5 guard refused `postgres:5432` **inside compose**, breaking Mode B's migrate | refined to host-local 5432 only; caught by rebuilding, 10 test cases |
| QA M1 | **an unintended edit to your governance file** — see §7 | restored; Markdown excluded from Ruff |
| QA re-review | the 5432 guard could be bypassed by `0.0.0.0:5432` (reached your local server; reproduced emitting `CREATE TABLE`), `127.0.0.2`, a trailing dot, IPv4-mapped loopback, and `?port=` / `?host=` query overrides | guard now resolves the **driver's actual target** (SQLAlchemy psycopg dialect) and classifies it with `ipaddress`; 18 test cases |
| QA re-review | `doctor` reported FAIL for a 5432 target, then connected to it anyway | stops before connecting; verified 0 connections, exit 1 |
| QA re-review | CI Postgres healthchecks had the same socket race as M3 | `pg_isready -h 127.0.0.1` |

---

## 6. Architecture impact

No change to the approved architecture. Every decision is recorded:

| ADR | Decision |
|---|---|
| 0001 | Monorepo, single multi-root Python distribution |
| 0002 | Colima as container runtime (licence-clean) |
| 0003 | pgvector provisioned, not enabled; native degraded mode supported |
| 0004 | Single driver — **asyncpg** (Apache-2.0); originally psycopg 3, replaced in the cleanup (§10) |
| 0005 | Worker is a tick loop; LangGraph/Playwright/etc. deferred |
| 0006 | Minimal schema: `events` only |
| 0007 | Hybrid topology; host Ollama preferred; compose Ollama opt-in |
| 0008 | Electron readiness — your rules; enforcement added in the cleanup (§10) |

No forbidden infrastructure (Qdrant, Neo4j, Redis, Kafka, Celery, Temporal), no paid API, no managed service. Dependencies match the approved plan, with three precise notes: **PyYAML** (MIT) is now declared directly for the policy-data files but was already installed as part of the approved `uvicorn[standard]`; **`vite`** and **`@testing-library/dom`** (both MIT) were not named in the plan but are required peers of the approved `vitest` and `@testing-library/react`. One extra compose service, `migrate`, is a one-shot job using the API image — not new infrastructure.

---

## 7. Actions taken on your machine — full disclosure

| Action | Scope | Reversible |
|---|---|---|
| `brew install colima docker docker-compose` | **approved** | `brew uninstall` |
| Created `~/.docker/config.json` (did not exist) with `cliPluginsExtraDirs` so `docker compose` works | required by the brew caveat | delete the file |
| Colima VM started (2 CPU, 4 GiB, 20 GiB) | — | `colima stop` / `colima delete` |
| Pulled `pgvector/pgvector:pg16` | **approved** | `docker rmi` |
| Pulled `python:3.12-slim` and `node:26-slim` (base images for the two Dockerfiles) | implied by "start the stack" | `docker rmi` |
| Volume `ken-interlink_postgres-data` (project DB, currently **running**) | — | `docker compose down -v` (destroys data) |
| Created `.env` from `.env.example` (gitignored) | what `make setup` does | delete it |
| Throwaway PG 16 cluster from Homebrew binaries in the session scratchpad, port 5434 | for the ADR-0003 check | **already deleted** |

**Not touched:** your Homebrew PostgreSQL 16 on 5432 and its `automation_dashboard` database — verified read-only before and after; neither database contains `alembic_version` or `events`. Ollama configuration unchanged, nothing pulled. No git commit, no remote, no push. No real CMS. No production credentials.

**Unintended edit, now reverted:** while fixing a lint gate I ran `ruff format .` over the whole repository. Ruff 0.17 also formats Python code blocks *inside Markdown*, and it rewrote a code block in your **`.claude/agents/backend-engineer.md`** (collapsing `...` stubs onto one line). The QA review caught it. I confirmed the diff contained only that reformat, restored the file with `git checkout --`, and excluded `*.md` and `.claude/` from Ruff so it cannot recur. Among tracked files, only `README.md` (intentionally rewritten) now differs from your initial commit.

---

## 8. Known limitations

- **Embedding role unsatisfied** — `/ready` is `degraded` in Mode A until Phase 2 pulls a model. Intended.
- **CI never executed** — no git remote; pushing is an ASK.
- **Native vector-less round-trip is not automatic** — static test runs everywhere; the executable check is CI-only (never run) or manual.
- **Mode B uses the mock model provider** — a container cannot reach the `127.0.0.1`-bound host Ollama without exposing it, which you chose not to require.
- **Starlette deprecation warning** about its test client and httpx — harmless today.
- **TypeScript response types mark always-present fields optional** (validation-mode OpenAPI); the client tolerates it.
- **Colima does not auto-start** after a reboot — run `colima start`.
- **Path resolution assumes an editable install.** `REPO_ROOT` (used for `.env` and `alembic.ini`) is derived from the source location, which holds for `pip install -e .` in dev and in both images. A non-editable wheel install would resolve under `site-packages`; this is unverified and flagged in ADR-0001.
- Remaining QA "record for later" items (heartbeat retention, href/XSS validation, DB-level append-only, RBAC on `/ready`, image digests, …) are in [NEXT_STEPS.md](NEXT_STEPS.md) with target phases.

---

## 9. Decisions needed from you

Decisions 1 (licence) and 2 (`skill.promoted`) from the original report were **resolved in the cleanup
pass** — see §10. Still open:

1. **Phase 1 inputs:** content source (public site / CMS read API / export) and crawl scope and
   politeness — see [NEXT_STEPS.md](NEXT_STEPS.md).
2. **Authorisation to begin Phase 1.**

---

## 10. Post-Phase-0 cleanup pass (2026-10-13)

A controlled cleanup authorised by the user, **not Phase 1**: make the repository licence-safe for
possible desktop distribution, close the `skill.promoted` gap, and make the UI Electron-ready.

### What changed

| Item | Change | Owner |
|---|---|---|
| 1. Database driver | psycopg 3 (LGPL-3.0) → **asyncpg 0.31.0** (Apache-2.0). Only `postgresql+asyncpg://` is accepted. There are no synchronous engines anywhere: integration tests use a `SyncDb` helper, and doctor uses the async engine. Connect timeout bounded at 5 s (asyncpg's default is 60 s). Alembic unchanged apart from the URL (its env was already async). | orchestrator |
| 1. Safety guard | Port-5432 guard ported to the asyncpg dialect. Porting found that asyncpg returns **lists** for multi-host URLs, which a naive port would have let through; now normalised and tested. | orchestrator |
| 2. Event vocabulary | `skill.promoted` added. `skill.proposed → offline/human evaluation → skill.promoted`. A governed `PromotionAuditPayload` (human `promoted_by`, `evaluation_ref`, version, previous version) is enforced for both `skill.promoted` and `policy.promoted` at construction, by a frozen envelope, **and at persistence**; the normalised payload is what is stored. | orchestrator |
| 3. Electron readiness | Your ADR-0008 rules kept verbatim and formalised. ESLint guard against `electron` in every import form (including `window.require`). `PlatformBridge` extended with a shape-guarded `window.kenDesktop` resolver; your API is unchanged. `apps/desktop/README.md` gained a security-requirements section. **Electron not installed.** | frontend-engineer |
| 4. Portability | API split into an environment-agnostic `lib/api-client.ts` (`createApiClient`) and a single env-reading `lib/api.ts`. Portability guard: `components/**` and all `lib/**` (except `lib/api.ts`) may not use Node built-ins, `process`, `server-only`, server-only Next.js APIs or DB clients. `/system` rendering is unchanged. | frontend-engineer |
| 5. Docs | ADR-0004 rewritten (psycopg kept as history), ADR-0008 formalised; CONTRACTS rules 11–12; ARCHITECTURE_OVERVIEW, DEVELOPMENT, README, skills/README, NEXT_STEPS. | orchestrator |

### Final verification (on the final code)

| # | Check | Result |
|---|---|---|
| 1 | `make verify` | **exit 0** |
| 2 | `make doctor` | 0 FAIL, 1 WARN (embedding model not pulled — intended) |
| 3 | Backend unit | **253 passed** (337 Python tests in total, 0 skipped) |
| 4 | Backend contract | **69 passed** (was 54) |
| 5 | Backend integration | **15 passed**, 0 skipped (strict DB gate) |
| 6 | Ruff | clean; 76 files formatted |
| 7 | mypy | 75 files, no issues |
| 8 | Frontend lint | clean, 0 warnings |
| 9 | Frontend typecheck | clean |
| 10 | Frontend tests | **286 passed** (was 46) |
| 11 | Compose validation | default / full / full+ollama-container all valid |
| 12 | Alembic up/down/up | on `ken_interlink_test` over asyncpg: table 1 → 0 → 1, revision tracked, only `plpgsql` |
| 13–15 | `/health` `/ready` `/meta` | Mode A: 200 / 200 `degraded` (embedding role only) / 200, no credentials. Mode B rebuilt: 200 / 200 `ready` / 200 |
| 16 | OpenAPI drift | live app == frozen `openapi.json`; `types.gen.ts` byte-identical after regeneration |
| 17 | Electron guard | a real component importing `electron` fails `npm run lint`; 202 guard tests pass; `npm ls electron` → empty |
| — | Live Ollama tier | 3/3 passed |
| — | Licence | **0 GPL/LGPL packages** in the Python tree; asyncpg Apache-2.0; no Electron or sharp in any runtime image |

### What the reviews caught in this pass

| Found by | Issue | Resolution |
|---|---|---|
| Orchestrator, while porting | asyncpg returns multi-host targets as **lists**; the string-based guard would have missed `localhost:5432` in the second slot | `_as_list` normalisation + test; verified via Alembic CLI |
| Orchestrator | The mechanical URL rewrite also flipped two *intentional* "rejected driver" test cases to the now-valid scheme | restored to `postgresql+psycopg://` |
| Orchestrator | A zsh word-splitting slip made the first CLI round-trip a no-op that *looked* green | caught by verifying table state at each step; re-run properly |
| QA **H1** (required) | promotion governance held only at construction: `model_copy`, assignment or in-place payload edits could persist an ungoverned promotion, after which `list_events` would fail forever | re-validation at persistence + frozen envelope; tests proven to fail without the fix |
| QA M2 | whitespace-only `promoted_by` accepted | `str_strip_whitespace` |
| QA M3 | unreachable DB hung the worker 61 s (asyncpg default) | connect timeout 5 s → measured 7–8 s. *One early measurement read 174 s; it did not reproduce in two exact re-runs, and a timestamped trace shows the mechanism.* |
| QA M4 | accidental lint-guard gaps: new `lib/` files, `next/headers` in components, `window.require("electron")`, `globalThis.process` | all blocked; legitimate server code still allowed |
| QA M5, L1, L2 | docs overstated the 5432 guard and lint guards; asyncpg statement-cache behaviour undocumented | docs now state exactly what is enforced, and what is not |
| QA re-review | a *refused* assignment still mutated the envelope; whitespace stripped for checking but stored unstripped; migrations still on asyncpg's 60 s connect default | `EventEnvelope` frozen (refusal leaves it unchanged); normalised payload stored; migrations bounded by the same connect timeout. QA **CONFIRMED** the cleanup may close. |

### Disclosure

- Your Electron files were preserved and extended; nothing of yours was replaced. The `PlatformBridge`
  interface is identical (now documented), and all three lines of your `apps/desktop/README.md` remain.
- Your local `.env` (created from `.env.example` in Phase 0) was updated to the asyncpg scheme.
- Throwaway containers were used to inspect image contents (auto-removed). No new images were pulled.
- Your PostgreSQL 16 on 5432 and `automation_dashboard` were not touched: no migration and no connection
  (doctor stops before connecting to a 5432 target).
- No git commit, no push, no PR, no real CMS, no paid API, no AUTO_APPROVE, no Electron install.
