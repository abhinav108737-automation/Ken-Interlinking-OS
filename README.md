<div align="center">

# Ken Research Agentic Interlinking OS

**A self-hosted, local-first system that finds, vets, publishes, verifies and measures contextual internal links, under strict human control.**

![Status](https://img.shields.io/badge/phases-0%E2%80%939%20implemented-2ea44f)
![Python](https://img.shields.io/badge/python-3.12-3776ab)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-336791)
![Qdrant](https://img.shields.io/badge/vector%20index-Qdrant-dc382d)
![Inference](https://img.shields.io/badge/inference-local%20(Ollama)-black)
![Licence](https://img.shields.io/badge/licence-proprietary-lightgrey)

</div>

Ken Research publishes thousands of market reports, articles and surveys. This system reads that content, works
out which pages should link to which, places each link on a natural phrase **already in the text**, and (only
when policy and people allow it) publishes, verifies and measures the result. Models propose and critique; they
never write to storage, call the CMS or promote anything. Everything runs on your own machine, with no paid API.

> **For the technical team running the compliance audit.** This README describes the repository as of
> **2026-10-30**. Every claim names the file, ADR, command or test where it can be checked; where something is
> *not* yet true, it says so (sections 9 and 11). **Nothing is connected to the production CMS, and nothing is published.**

## Highlights

- **Deterministic first.** Retrieval, scoring, eligibility, placement, safety checks and deployment are plain code. Models are optional reviewers behind a strict, validated schema.
- **Safe by construction.** Hard safety rules, protected pages, version locks, a verified splice (one `<a>` inserted, every other byte identical) and rollback for every change.
- **Autonomy you control.** `shadow` (default) records what *would* happen; `guarded` and `autonomous` are opt-in and refused against a production CMS without extra acknowledgement.
- **Local and private.** PostgreSQL, Qdrant and Ollama on loopback. Google Analytics and Search Console are optional and read-only.
- **Auditable.** 25 architecture decision records, a frozen contract package, typed events, about 6,200 automated tests and evaluation datasets.

## Quick start

### Windows 11 (PowerShell)

Requires Python 3.12, Node 22/24+, Git, [Docker Desktop](https://www.docker.com/products/docker-desktop/) and [Ollama](https://ollama.com/download).
`make` is not used on Windows; every command below is the plain equivalent.

```powershell
# 1. Get the code and install dependencies
git clone https://github.com/abhinav108737-automation/Ken-Interlinking-OS.git
cd Ken-Interlinking-OS
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\pip.exe install -e ".[dev]" -c requirements.lock.txt
npm ci

# 2. Configure (use 127.0.0.1, not localhost: Windows resolves localhost to IPv6 and the database will time out)
Copy-Item .env.example .env
(Get-Content .env) -replace 'localhost','127.0.0.1' | Set-Content .env

# 3. Start PostgreSQL and Qdrant, then create the schema
docker compose up -d postgres qdrant
.venv\Scripts\alembic.exe upgrade head
ollama pull nomic-embed-text

# 4. Run the API (terminal 1)
.venv\Scripts\uvicorn.exe ken_api.main:app --host 127.0.0.1 --port 8000

# 5. Run the dashboard (terminal 2)
cd apps\web
$env:NEXT_TELEMETRY_DISABLED = "1"
..\..\node_modules\.bin\next.cmd dev --hostname 127.0.0.1 --port 3000
```

> `npm run dev` does not work on Windows because its script uses POSIX `VAR=1 command` syntax; use the `next.cmd` line above.

Create your account (you are asked for a password; no account exists until you do), then open
**http://127.0.0.1:3000**:

```powershell
.venv\Scripts\python.exe -m ken_services.auth.cli user-create --username you --role operator_admin --display-name "Your Name"
```

Load the synthetic demo site and run the pipeline (nothing is published at any step):

```powershell
.venv\Scripts\python.exe -m ken_services.inventory.cli import-file tests\fixtures\demo\research_site.jsonl
.venv\Scripts\python.exe -m ken_services.intelligence.cli analyze          # classify, embed, relate
.venv\Scripts\python.exe -m ken_services.recommendations.cli recommend     # propose links
.venv\Scripts\python.exe -m ken_agents.cli agents                          # local model reviews them
```

### macOS / Linux

```bash
colima start                       # or Docker Desktop
make setup                         # venv, dependencies, .env
make dev                           # PostgreSQL + Qdrant + migrations
make dev-api                       # terminal 1: http://127.0.0.1:8000/ready
make dev-web                       # terminal 2: http://127.0.0.1:3000
make ingest-file FILE=tests/fixtures/demo/research_site.jsonl
make analyze && make recommend && make agents
make user-create USERNAME=you ROLE=operator_admin
make verify                        # the full quality gate
```

### What you can open

| URL | What it shows |
|---|---|
| `http://127.0.0.1:3000/inventory` | Every page, its versions and the links it already contains |
| `http://127.0.0.1:3000/intelligence` | Page type, market, geography, relationships, eligibility |
| `http://127.0.0.1:3000/recommendations` | Proposed link plans with rationale, score breakdown and before/after preview |
| `http://127.0.0.1:3000/agents` | Every agent run, step and model call |
| `http://127.0.0.1:3000/jobs` | Job runs and their counters |
| `http://127.0.0.1:8000/docs` | Interactive API reference |

More detail: [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md). The current evaluation of the engine's output is in
[docs/evaluation/](docs/evaluation/INTERLINK_BASELINE_2026-10-30.md).

## Contents

1. [At a glance](#1-at-a-glance) · 2. [How to read this repository](#2-how-to-read-this-repository) · 3. [Architecture](#3-architecture) · 4. [Runtime topology](#4-runtime-topology)
5. [Control framework](#5-control-framework-constraint--enforcement--evidence) · 6. [Data handling and privacy](#6-data-handling-and-privacy) · 7. [Security posture](#7-security-posture)
8. [Verification](#8-verification-how-to-reproduce) · 9. [Known limitations](#9-known-limitations-stated-openly) · 10. [Data holdings](#10-data-holdings-and-the-real-site-bootstrap-adr-0025)
11. [What the implementing team must provide](#11-what-the-implementing-team-must-provide-to-move-beyond-local) · 12. [Repository layout](#12-repository-layout) · 13. [Licensing](#13-licensing-and-provenance) · 14. [Documentation index](#14-documentation-index)

---

## 1. At a glance

| | |
|---|---|
| **Purpose** | Observe → understand → recommend → decide → publish → verify → measure → learn, for internal body links |
| **Status** | Phases 0–9 implemented and verified locally; reports in §10. Not deployed. Not connected to the production CMS |
| **Backend** | Python 3.12 · FastAPI 0.143 · SQLAlchemy 2.1 (asyncio) + asyncpg · Alembic · Pydantic 2.14 · LangGraph 1.2 (`StateGraph` only) |
| **Frontend** | Next.js 16.4 · React 19.3 (server-rendered; no direct database access) |
| **System of record** | PostgreSQL 16, 10 Alembic migrations (head `0010_qdrant_and_autonomy`) |
| **Vector index** | Qdrant v1.19.1, a **derived** index that can always be rebuilt from PostgreSQL ([ADR-0017](docs/adr/ADR-0017-qdrant-vector-architecture.md)) |
| **Models** | Local only, through Ollama behind a provider abstraction: reasoning `qwen3:8b-q4_K_M`, fast `qwen3:4b`, embeddings `nomic-embed-text` |
| **Paid APIs / SaaS** | None required. Google Analytics 4 and Search Console are optional, **read-only** and off unless enabled |
| **Governance** | 25 architecture decision records ([docs/adr/](docs/adr/)), a frozen contract package, a 20-point constraint list ([CLAUDE.md](CLAUDE.md)) |
| **Verification** | About 6,200 automated tests plus evaluation datasets; ruff and mypy clean (§8) |
| **Licence** | Proprietary (`pyproject.toml`). Third-party licences are permissive; see §13 |

---

## 2. How to read this repository

Read in this order. The first five are the **governing specification**; code must comply with them.

1. [START_HERE.md](START_HERE.md): project brief
2. [PRD.md](PRD.md) and [TRD.md](TRD.md): product and technical requirements
3. [CLAUDE.md](CLAUDE.md): the 20 non-negotiable constraints and the ALLOW / ASK rules (§5 maps each to its enforcement)
4. [AUTONOMY_AND_PERMISSIONS.md](AUTONOMY_AND_PERMISSIONS.md): the ALLOW / ASK / BLOCK permission model
5. [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md): the phases
6. [docs/adr/](docs/adr/): *why* each architectural choice was made (decisions that depart from the specification are recorded here, not made silently)
7. [docs/CONTRACTS.md](docs/CONTRACTS.md): the frozen shared contracts; [NEXT_STEPS.md](NEXT_STEPS.md): open decisions and known limitations

---

## 3. Architecture

### 3.1 The one rule that shapes everything

Probabilistic reasoning must always end at a **typed, deterministic boundary** before any irreversible action
(TRD §24):

```
LLM → structured decision → policy → deterministic validation → authorisation
    → deterministic execution → measurement → learning
```

Models propose and critique. They never write to storage, call the CMS, or promote anything.

### 3.2 Components

```mermaid
flowchart TD
    UI["Next.js UI<br/>apps/web"] -->|server-side fetch| API["FastAPI Control API<br/>apps/api / ken_api"]
    API --> SVC["Deterministic services<br/>inventory, intelligence, recommendations,<br/>deployment, analytics, autonomy"]
    API --> AG["LangGraph agents<br/>review plans only"]
    API --> LRN["Learning and evaluation<br/>reflect, propose, offline eval, shadow, human promotion"]
    SVC --> CORE["ken_core<br/>settings, db, providers, repositories"]
    AG --> CORE
    LRN --> CORE
    CORE --> PG[("PostgreSQL 16<br/>system of record")]
    CORE --> QD[("Qdrant<br/>derived vector index")]
    CORE --> OL["Ollama<br/>local models"]
    CORE --> CMS["CMSProvider<br/>mock / simulator<br/>real HTTP adapter disabled"]
```

| Layer | Package | Rule |
|---|---|---|
| Contracts | `ken_contracts` | Frozen Pydantic models and protocols; imports nothing internal |
| Platform | `ken_core` | Settings, logging, DB, ORM, repositories, provider implementations |
| Deterministic services | `ken_services` | All business logic that can be deterministic is |
| Control API | `ken_api` | Thin HTTP layer; about 70 routes, almost all read-only (§7) |
| Agent runtime | `ken_agents` | Supervised LangGraph workflow and worker loop |
| UI | `apps/web` | Never imports Electron, never touches the database ([ADR-0008](docs/adr/ADR-0008-electron-readiness.md)) |

Dependencies point **inward only**: `ken_api → ken_services → ken_core → ken_contracts`.

### 3.3 The pipeline, by phase

| Phase | Capability | Report |
|---|---|---|
| 0 | Contracts, event log, platform, health | [PHASE_0](PHASE_0_REPORT.md) |
| 1 | Inventory: JSONL export, polite crawler, read-only CMS adapter; hash-versioned pages | [PHASE_1](PHASE_1_REPORT.md) |
| 2 | Content intelligence: taxonomy policy, page type / intent / market / geography, embeddings, relationships, eligibility | [PHASE_2](PHASE_2_REPORT.md) |
| 3 | Recommendations: semantic, lexical, taxonomy and graph channels → immutable `LinkPlan`s with rationale | [PHASE_3](PHASE_3_REPORT.md) |
| 4 | Supervised agents that **review** plans | [PHASE_4](PHASE_4_REPORT.md) |
| 5 | Sign-in, roles, dashboard, review inbox, structured feedback | [PHASE_5](PHASE_5_REPORT.md) |
| 6 | Controlled deployment: validate → dry-run → version-locked apply → verify → roll back | [PHASE_6](PHASE_6_REPORT.md) |
| 7 | Analytics: first-party beacon, GA4, Search Console, outcome attribution | [PHASE_7](PHASE_7_REPORT.md) |
| 8 | Learning: eval datasets, offline eval, skill and policy lifecycles, experiments | [PHASE_8](PHASE_8_REPORT.md) |
| 9 | Autonomy policy engine (`shadow` / `guarded` / `autonomous`); Qdrant | [PHASE_9](PHASE_9_REPORT.md) |

---

## 4. Runtime topology

All listeners bind to **loopback only** (`docker-compose.yml`, `.env.example`).

| Service | Address | Notes |
|---|---|---|
| PostgreSQL 16 (pgvector image) | `127.0.0.1:5433` | Not 5432: another local PostgreSQL may own it. `make doctor` guards this |
| Qdrant v1.19.1 | `127.0.0.1:6333` / `6334` | Image pinned by digest |
| Ollama | `127.0.0.1:11434` | Host daemon; never exposed on `0.0.0.0` ([ADR-0007](docs/adr/ADR-0007.md)) |
| FastAPI | `127.0.0.1:8000` | `GET /health`, `/ready`, `/meta`, `/docs` |
| Next.js | `127.0.0.1:3000` | |
| First-party analytics collector | its own loopback port | Never part of the control API |

---

## 5. Control framework: constraint → enforcement → evidence

The 20 constraints in [CLAUDE.md](CLAUDE.md) are the compliance baseline. The table maps each to where it is
enforced and how an auditor can check it. "Evidence" is a pointer, not a claim of independent verification.

| # | Constraint | Enforced by | Evidence / how to check |
|---|---|---|---|
| 1, 3 | No mandatory paid API; no OpenAI / Anthropic / Gemini / managed vector DB / paid SaaS | Dependency list; model provider is `ollama` or `mock`; Google is optional | `pyproject.toml` dependencies; `KEN_MODEL_PROVIDER`; [ADR-0004](docs/adr/ADR-0004.md) |
| 2, 6 | Local / self-hosted inference; Ollama first | `ken_core/providers/model/` | Provider tests; `.env.example` |
| 4, 5 | PostgreSQL is the record; vector layer is replaceable | Qdrant is derived and rebuildable (`make qdrant-rebuild`, `qdrant-verify`) | [ADR-0017](docs/adr/ADR-0017-qdrant-vector-architecture.md). *This replaced the original "pgvector first" choice; the decision is recorded, not silent* |
| 7 | LangGraph supervisor | `ken_agents`, `StateGraph` only (no checkpointer, no tracing; tracing is refused at start-up) | [ADR-0012](docs/adr/ADR-0012-agent-orchestration.md) |
| 8 | Deterministic code over LLM wherever possible | Candidate retrieval, scoring, eligibility, placement, safety and deployment are deterministic | [ADR-0011](docs/adr/ADR-0011-recommendation-engine.md) |
| 9 | Every cross-boundary LLM output validated with Pydantic | `ken_contracts`; strict output schemas with a deterministic fallback | `tests/unit/learning/test_reflection_model_boundary.py` and the agent tests |
| 10, 11 | No LLM writes to production; changes only through a controlled CMS adapter | Agents hold typed, allow-listed tools and no credentials; CMS provider is `mock`; real writes need `KEN_CMS_WRITES_ENABLED` plus `KEN_CMS_ALLOW_REMOTE` | [ADR-0012](docs/adr/ADR-0012-agent-orchestration.md), [ADR-0014](docs/adr/ADR-0014-controlled-deployment.md) |
| 12 | Every mutation idempotent, version-aware, auditable, reversible | Operation ids, version locks, typed events, rollback, durable deployment intent (`make deployments-reconcile`) | Phase 6 tests; [ADR-0014](docs/adr/ADR-0014-controlled-deployment.md) |
| 13 | Every schema change has an Alembic migration | `migrations/versions/0001`–`0010`; `tests/integration/test_migrations.py` (upgrade → downgrade → upgrade) | `alembic history` |
| 14 | Every endpoint has tests | `tests/unit/test_api_*.py`, `tests/integration/` | `make test` |
| 15 | Every material agent capability has evaluation fixtures | `evals/datasets/` (12 datasets), `evals/suites/`, recorded model fixtures | [evals/README.md](evals/README.md) |
| 16 | Agents may propose policies / skills, never self-promote | Promotion is a human control-plane action (`operator_admin` only); the database refuses a `*.promoted` event without a human actor; demonstration mode currently refuses every promotion | [ADR-0016](docs/adr/ADR-0016-learning.md) |
| 17 | No silent architecture changes | Changes carry an ADR (25 so far) | `docs/adr/` |
| 18, 19 | Reference repos not cloned into source; licences checked | None vendored; two reference projects are licence hazards and are flagged | [docs/research/INTERLINKING_REFERENCE_REPOS.md](docs/research/INTERLINKING_REFERENCE_REPOS.md); [NEXT_STEPS.md](NEXT_STEPS.md) |
| 20 | No secrets in source control | `.gitignore` (`keys/`, `*-service-account*.json`, `.env`); a test fails the build if a credential is tracked; credential files must live outside the repo | commit `b5287d2`; [ADR-0022](docs/adr/ADR-0022-google-analytics-authentication.md) |

### 5.1 Permission model (ALLOW / ASK / BLOCK)

Defined in [AUTONOMY_AND_PERMISSIONS.md](AUTONOMY_AND_PERMISSIONS.md). In short:

- **ASK** (needs explicit human approval): pushing to a remote, real CMS writes, real write credentials,
  destructive DB actions, enabling auto-publishing, promoting a policy or skill, a new infrastructure
  service, calling a paid or external API, live deployment.
- **BLOCK** (never, unless policy changes): mandatory paid API, direct LLM writes to production,
  silent self-promotion, unlogged production mutation, irreversible publication without rollback metadata,
  committed credentials, bypassing protected-page policy.

### 5.2 Autonomy modes ([ADR-0018](docs/adr/ADR-0018-autonomy-first-publishing.md))

`KEN_AUTONOMY_MODE` is `shadow` (default) | `guarded` | `autonomous`.

- `shadow` records what *would* happen and publishes nothing.
- `autonomous` is refused unless `KEN_AUTONOMY_AUTONOMOUS_ACKNOWLEDGED=true`, and refused against an `http`
  CMS with writes enabled unless `KEN_AUTONOMY_ALLOW_PRODUCTION=true` (a `Settings` validator, `ken_core/config.py`).
- The legacy `KEN_AUTO_APPROVE_ENABLED` is retired and refuses `true`.
- Decision order: deterministic hard blocks first; a critic can only decline or tighten; risk can only rise;
  calibration can only lower confidence; mode floors can only block.
- People are the **control plane** (overrides, promotion, rollback), not a per-link approval step.

### 5.3 Roles ([ADR-0013](docs/adr/ADR-0013-reviewer-control-plane.md))

`viewer`, `reviewer`, `operator_admin`; deny-by-default routes; no default accounts (create one with
`make user-create`). Actors come from the session, never from the request body.

---

## 6. Data handling and privacy

| Data | Where | Notes |
|---|---|---|
| Page content and versions | PostgreSQL | Immutable, hash-based version history |
| Embeddings | PostgreSQL (values) + Qdrant (index) | Local model; nothing is sent outside the machine |
| Events / audit | PostgreSQL `events` | Typed payloads; **not yet append-only at the database level** (§9) |
| Real-site exports | `data/` (git-ignored) | Public site content, regenerable, not committed |
| Credentials | Outside the repository | Never in `.env.example`; the service-account key path is configuration only |
| Analytics beacon | First-party collector | Stores **no IP, user agent, page-view id, referrer or cookie**; honours Global Privacy Control, Do Not Track and a consent flag |
| Google GA4 / Search Console | Egress, read-only | Sends only a property id and dates. Off unless `KEN_ANALYTICS_GOOGLE_ENABLED=true` |

Retention periods are recorded in policy but **no purge job enforces them yet** (§9). Whether the beacon needs
a consent mechanism under ePrivacy, GDPR or India's DPDP Act is an open legal decision (NEXT_STEPS item 17).

---

## 7. Security posture

**Implemented**
- Loopback-only binds; default `ken:ken` credentials refused in staging and production.
- Deny-by-default RBAC; per-account lockout; validation errors never echo input values.
- Crawler: `robots.txt` always honoured, GET only, no cookies, 1 s delay, at most 2 requests in flight,
  explicit site allow-list, identified user-agent.
- Database guards: immutable `page_versions` and `existing_links`; a `page_protections` trigger; a deferred
  constraint trigger on link-plan status transitions; a CHECK-capped holdout share for experiments.
- Deployment writes are re-validated under lock, spliced deterministically (one `<a>` inserted, every other
  byte identical), verified after publication and reversible.
- Google calls are refused before any request unless explicitly enabled; token exchange goes through the
  project's own guarded HTTP client.

**Known limitations an auditor should see** are listed in §9 rather than hidden.

---

## 8. Verification: how to reproduce

### 8.1 Prerequisites

Python **3.12**, Node `^22.22.2 || ^24.15.0 || >=26`, Docker (Colima or Docker Desktop), Ollama with
`nomic-embed-text` (and optionally `qwen3:4b`, `qwen3:8b-q4_K_M`). Details: [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

### 8.2 Run

Follow the [Quick start](#quick-start). To run the quality gate:

```bash
make verify        # macOS / Linux: lint, types, tests, web tests, compose config, migrate, doctor
```

```powershell
# Windows
$env:KEN_TEST_DATABASE_URL = "postgresql+asyncpg://ken:ken@127.0.0.1:5433/ken_interlink_test"
.venv\Scripts\ruff.exe check services contracts agents apps evals tests
.venv\Scripts\mypy.exe
.venv\Scripts\python.exe -m pytest tests -q
```

Use `127.0.0.1`, not `localhost`: on Windows the integration tests **skip silently** otherwise. The
`ken_interlink_test` database is created by the container's init script on first start.

### 8.3 Test layers

`tests/unit` · `tests/contract` (frozen-contract tests) · `tests/integration` (real PostgreSQL) · `evals/`
(datasets and recorded-model suites) · `apps/web` (vitest).

### 8.4 Most recent results (2026-10-29/30, Windows)

| Check | Result |
|---|---|
| `ruff check` | clean |
| `mypy` (strict on `ken_agents`) | clean, 508 source files |
| Full suite (excluding `tests/e2e`) | 6,187 passed, 37 skipped, **3 failed**; the 3 were then fixed or shown load-sensitive and re-run (`PHASE_8_REPORT.md` §4) |
| Phase 8 learning | 863 unit/contract + 174 integration passed |
| Phase 9 autonomy | 336 passed |

The 37 skips are platform limits (POSIX file modes, signal handlers, `make`). **CI has never run**: the workflow
exists (`.github/workflows/ci.yml`) but the repository has no remote.

---

## 9. Known limitations (stated openly)

| Area | Limitation | Where recorded |
|---|---|---|
| Not production-ready | No production CMS integration; parity with Ken's real page templates unproven | NEXT_STEPS items 12, 14 |
| Database role | The app connects as superuser `ken` locally; production needs a least-privilege role (DML only) | NEXT_STEPS "Hard gates" |
| Audit log | `events` is not append-only in the database (`page_versions` and `existing_links` are) | NEXT_STEPS |
| Web security | CSP allows `'unsafe-inline'` (nonces not yet generated); account lockout can be triggered by anyone who knows a username | PHASE_5_REPORT |
| Analytics | First-party counts are spoofable; outcomes are correlational only; retention is recorded but not enforced | PHASE_7_REPORT |
| Autonomy | No calibration exists, so `guarded` mode would publish nothing on a fresh deployment | PHASE_9_REPORT |
| Learning | Demonstration mode refuses all promotions; the learning corpus has no real human review actions yet | PHASE_8_REPORT |
| Taxonomy | The real-site taxonomy is **proposed** (v3, 5,451 markets derived from URL slugs), needs owner review, and its tag rate measures coverage, not correctness; the company / product list is empty | ADR-0025, NEXT_STEPS 26 |
| Inventory completeness | The sitemaps list about 9,000 pages, but analytics shows about 65,000 site paths with traffic; a sample suggests roughly 40,000 live pages are missing from the inventory | ADR-0025 addendum, NEXT_STEPS 24 |
| Scale | Similarity search is an exact scan per source page in Python: measured 2.3 s per page at 8,858 pages (hours for a full run). An ANN or batched path is needed before full-site recommendation | NEXT_STEPS 25 |
| CI | Authored, never executed | NEXT_STEPS |
| Platform | Developed on macOS and Windows; some tests skip on Windows | PHASE_8_REPORT |

---

## 10. Data holdings and the real-site bootstrap ([ADR-0025](docs/adr/ADR-0025-real-site-bootstrap.md))

With the owner's written approval (2026-10-29), the public sitemaps of `www.kenresearch.com` were fetched
**read-only** (robots-honouring, identified user-agent, 1 s delay, at most 2 requests in flight):

| Database | Content | Purpose |
|---|---|---|
| `ken_interlink` | Synthetic demo site (about 65 pages) | Development and tests; never mixed with real data |
| `ken_real500` | 498 pages, Playwright-rendered | First real run: analysis, 126 link plans, shadow autonomy, live Google check |
| `ken_full` | 8,858 pages, raw HTML from every non-disallowed sitemap URL (about 18 % of the live site, see §9) | Full sitemap inventory; analysed under taxonomy v3 without the semantic step |

Nothing was written to the website. Google Analytics 4 and Search Console were read live (read-only, one
week of data).

---

## 11. What the implementing team must provide (to move beyond local)

These are **ASK-level** or external dependencies. None is done; each needs the owner's explicit approval.

1. **CMS integration**: base URL, link-change contract (must tell a version conflict 409 from a reused
   operation id), sanitiser behaviour for `data-*` attributes, and write credentials stored outside source
   control ([docs/handover/CMS_INTEGRATION_REQUIREMENTS.md](docs/handover/CMS_INTEGRATION_REQUIREMENTS.md)).
2. **Least-privilege database role**, TLS and network isolation for any shared environment.
3. **Secrets management** for the CMS and Google credentials; rotation procedure
   ([docs/handover/GOOGLE_ANALYTICS_SETUP.md](docs/handover/GOOGLE_ANALYTICS_SETUP.md)).
4. **Nonce-based CSP**, and a decision on lockout behaviour if exposed beyond a trusted network.
5. **Consent decision** for the analytics beacon (legal), and the production home of first-party collection.
6. **Real-template parity check** on 10–20 real page bodies before any deployment.
7. **Calibration data** and real editorial decisions before `guarded` / `autonomous` mode can mean anything.
8. **CI** on a hosted remote, and an audit-log hardening pass (append-only `events`).
9. **Owner review** of the proposed taxonomy and the company / product list.

The full, current list of open decisions is [NEXT_STEPS.md](NEXT_STEPS.md).

---

## 12. Repository layout

```
apps/           api (FastAPI) · web (Next.js) · desktop (reserved, not installed)
contracts/      ken_contracts: frozen Pydantic models and protocols
services/       ken_core (platform) · ken_services (inventory, intelligence, recommendations,
                deployment, review, analytics, learning, autonomy, auth, vectors, graph, jobs, agents)
agents/         ken_agents: LangGraph workflow and worker
migrations/     Alembic versions 0001–0010
policies/       Versioned policy data: taxonomy (v1 default, v2 proposed), ranking, recommendation,
                autonomy, analytics, learning, graph, agents
skills/         Reserved for approved skills (none promoted; promotion is human-only)
evals/          Datasets, suites, runners, derivation tools
tests/          unit · contract · integration · fixtures
docs/           ADRs, contracts, development guide, handover notes, research
infrastructure/ Container build files and helper scripts
workflows/      Reserved for workflow definitions (README only)
data/           Git-ignored local exports (real-site content)
```

---

## 13. Licensing and provenance

- This project: **proprietary** (`pyproject.toml`).
- Dependencies are pinned and permissive (MIT, BSD, Apache-2.0). Choices that avoided restrictive licences
  are documented in `pyproject.toml` (for example, no LangGraph Postgres checkpointer because it pulls an
  LGPL driver). The `sharp` image library is LGPL and must be re-checked before any packaged desktop bundle.
- **Two research references are licence hazards and must not be copied from:** `searchsolved/search-solved-public-seo`
  (CC BY-NC 4.0, non-commercial) and `Deepak-J0shi/SEO-Interlinking-Tool` (no licence declared). Nothing was
  copied; influence is conceptual only. See [NEXT_STEPS.md](NEXT_STEPS.md) and
  [docs/research/INTERLINKING_REFERENCE_REPOS.md](docs/research/INTERLINKING_REFERENCE_REPOS.md).
- Models: `qwen3` (Apache-2.0) and `nomic-embed-text` (Apache-2.0), run locally.

---

## 14. Documentation index

| Document | Purpose |
|---|---|
| [START_HERE.md](START_HERE.md), [PRD.md](PRD.md), [TRD.md](TRD.md) | Brief and governing requirements |
| [CLAUDE.md](CLAUDE.md), [AUTONOMY_AND_PERMISSIONS.md](AUTONOMY_AND_PERMISSIONS.md) | Constraints and permission model |
| [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md), `PHASE_0…9_REPORT.md` | Plan and evidence per phase |
| [NEXT_STEPS.md](NEXT_STEPS.md) | Open decisions, limitations, approvals needed |
| [docs/ARCHITECTURE_OVERVIEW.md](docs/ARCHITECTURE_OVERVIEW.md), [docs/CONTRACTS.md](docs/CONTRACTS.md) | Architecture and frozen contracts |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Setup and daily workflow |
| [docs/adr/](docs/adr/) | Decision records (ADR-0001 … ADR-0025) |
| [docs/handover/](docs/handover/) | CMS and Google Analytics integration requirements |
| [evals/README.md](evals/README.md) | Evaluation datasets and suites |
