# =============================================================================
# Ken Research Agentic Interlinking OS — developer entrypoints
#
# Every Python command uses .venv/bin/ explicitly: the system python3 on the
# development machine is 3.9, and SQLAlchemy 2.1 requires 3.11+.
#
# Preferred Mac loop (Mode A, docs/adr/ADR-0007.md):
#   make setup        once
#   make dev          start Postgres (container, :5433) and migrate
#   make dev-api      terminal 1 — FastAPI on 127.0.0.1:8000
#   make dev-web      terminal 2 — Next.js on 127.0.0.1:3000
#   make verify       the Phase gate
# =============================================================================

PYTHON_BOOTSTRAP ?= /opt/homebrew/bin/python3.12
VENV             := .venv/bin
COMPOSE          := docker compose
export NEXT_TELEMETRY_DISABLED := 1

.DEFAULT_GOAL := help
.PHONY: help setup lock db-up db-down migrate db-roles db-roles-check pilot-db-create pilot-preflight pilot-robots pilot-crawl offline-demo offline-demo-fast offline-demo-report import-html-dir db-roles-test dev dev-api dev-web \
        test test-integration test-qdrant test-all test-live web-test lint format types \
        compose-config compose-full-up compose-full-down doctor verify openapi \
        ingest-file ingest-cms-mock crawl analyze recommend agents \
        user-create user-list user-disable user-enable user-set-role user-set-password \
        sessions-prune deployments-reconcile jobs-reap cms-sim-seed cms-sim-serve \
        analytics-import analytics-aggregate analytics-demo-seed analytics-sim-serve \
        reflect eval shadow policy-promote experiment-create experiment-assign \
        qdrant-up qdrant-down qdrant-index qdrant-rebuild qdrant-verify graph-metrics \
        autonomy autonomy-shadow autonomy-run autonomy-dry-run autonomy-legacy \
        demo demo-clean

help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "} {printf "  %-18s %s\n", $$1, $$2}'

# ---------------------------------------------------------------- setup
setup: ## Create .venv (Python 3.12), install Python + Node dependencies
	@test -x $(VENV)/python || $(PYTHON_BOOTSTRAP) -m venv .venv
	$(VENV)/python -m pip install --quiet --upgrade pip
	$(VENV)/pip install --quiet -e ".[dev]" -c requirements.lock.txt
	npm ci
	@test -f .env || cp .env.example .env
	@echo "setup complete — edit .env if needed, then: make dev"

lock: ## Regenerate requirements.lock.txt from the current venv (review the diff)
	$(VENV)/pip freeze --exclude-editable | grep -v '^ken-interlink' > requirements.lock.txt
	@echo "requirements.lock.txt updated — review with: git diff requirements.lock.txt"

# ---------------------------------------------------------------- database
db-up: ## Start the project Postgres container on 127.0.0.1:5433 and wait for healthy
	$(COMPOSE) up -d postgres
	@for i in $$(seq 1 30); do \
	  s=$$(docker inspect -f '{{.State.Health.Status}}' ken-interlink-postgres-1 2>/dev/null); \
	  [ "$$s" = "healthy" ] && echo "postgres healthy on 127.0.0.1:5433" && exit 0; sleep 2; \
	done; echo "postgres did not become healthy" >&2; exit 1

db-down: ## Stop the Postgres container (keeps its data volume)
	$(COMPOSE) stop postgres

migrate: ## Apply Alembic migrations as the OWNER (KEN_MIGRATION_DATABASE_URL, else KEN_DATABASE_URL)
	$(VENV)/alembic upgrade head

# ------------------------------------------------- vector store (ADR-0017)
# Qdrant is a DERIVED index: PostgreSQL keeps the embedding values, so every
# target here is safe to re-run and `qdrant-rebuild` can always reconstruct the
# collection from scratch. The container binds 127.0.0.1:6333/6334 only.
#
# The index/rebuild/verify bodies resolve `ken_services.vectors.cli`, which the
# vector workstream lands; until then they fail with a module-not-found, like
# the staged readers of earlier phases.
qdrant-up: ## Start the Qdrant container on 127.0.0.1:6333/6334 and wait for healthy
	$(COMPOSE) up -d qdrant
	@for i in $$(seq 1 30); do \
	  s=$$(docker inspect -f '{{.State.Health.Status}}' ken-interlink-qdrant-1 2>/dev/null); \
	  [ "$$s" = "healthy" ] && echo "qdrant healthy on 127.0.0.1:6333" && exit 0; sleep 2; \
	done; echo "qdrant did not become healthy" >&2; exit 1

qdrant-down: ## Stop the Qdrant container (keeps its storage volume)
	$(COMPOSE) stop qdrant

# KEN_VECTOR_STORE defaults to `memory` (so the test suite needs no container). These
# targets exist to maintain the REAL index, so they pin `qdrant`: a process-local store
# would be filled and discarded in the same breath.
qdrant-index: ## Idempotent incremental indexing from PostgreSQL (ARGS="--force")
	KEN_VECTOR_STORE=qdrant $(VENV)/python -m ken_services.vectors.cli index $(ARGS)

qdrant-rebuild: ## Drop the collection and refill it from PostgreSQL
	KEN_VECTOR_STORE=qdrant $(VENV)/python -m ken_services.vectors.cli rebuild $(ARGS)

qdrant-verify: ## Drift report in both directions; non-zero exit on drift
	KEN_VECTOR_STORE=qdrant $(VENV)/python -m ken_services.vectors.cli verify $(ARGS)

# ----------------------------------------------- link-graph metrics (directive s5)
graph-metrics: ## Compute internal link-graph metrics into page_graph_metrics (deterministic)
	$(VENV)/python -m ken_services.graph.cli compute $(ARGS)

# ---------------------------------------------------------------- run
dev: db-up qdrant-up migrate ## Start Postgres + Qdrant + migrate, then run dev-api and dev-web
	@echo ""; echo "Now run, in two terminals:"; echo "  make dev-api"; echo "  make dev-web"

dev-api: ## FastAPI natively on 127.0.0.1:8000 (host Ollama, container Postgres)
	$(VENV)/uvicorn ken_api.main:app --reload --no-access-log --host 127.0.0.1 --port 8000

dev-web: ## Next.js natively on 127.0.0.1:3000
	npm run dev -w apps/web

# ---------------------------------------------------------------- quality gates
test: ## Unit + contract tests (no database, no network)
	$(VENV)/pytest -m "not integration and not live"

test-integration: ## Integration tests (needs Postgres on :5433; FAILS if unreachable)
	KEN_REQUIRE_INTEGRATION_DB=1 $(VENV)/pytest -m integration

test-qdrant: ## Tests that need the REAL Qdrant container (make qdrant-up); FAILS, never skips, if it is down
	KEN_REQUIRE_INTEGRATION_DB=1 KEN_REQUIRE_QDRANT_TESTS=1 $(VENV)/pytest -m qdrant

test-all: ## Everything except live tests; FAILS if the test DB or real Qdrant is unreachable
	KEN_REQUIRE_INTEGRATION_DB=1 KEN_REQUIRE_QDRANT_TESTS=1 $(VENV)/pytest -m "not live"

test-live: ## Live tests against the host Ollama daemon (opt-in, slow)
	KEN_RUN_LIVE_OLLAMA=1 $(VENV)/pytest -m live

web-test: ## Frontend unit/component tests
	npm run test -w apps/web

lint: ## Ruff (lint + format check) and ESLint
	$(VENV)/ruff check .
	$(VENV)/ruff format --check .
	npm run lint -w apps/web

format: ## Apply Ruff formatting
	$(VENV)/ruff format .

types: ## mypy (strict on contracts + core) and tsc --noEmit
	$(VENV)/mypy
	npm run typecheck -w apps/web

openapi: ## Regenerate contracts/openapi/openapi.json and apps/web/lib/types.gen.ts
	$(VENV)/python infrastructure/scripts/export_openapi.py
	npm run gen:types -w apps/web

# ---------------------------------------------------------------- inventory (Phase 1)
ingest-file: ## Import a JSONL export: make ingest-file FILE=path/to/export.jsonl
	@test -n "$(FILE)" || (echo "usage: make ingest-file FILE=path/to/export.jsonl" >&2; exit 2)
	$(VENV)/python -m ken_services.inventory.cli import-file "$(FILE)"

ingest-cms-mock: ## Import the mock CMS seed pages (no real CMS): make ingest-cms-mock
	$(VENV)/python -m ken_services.inventory.cli import-cms-mock --site-url https://example.com/

analyze: ## Phase 2: analyse, embed and relate stale pages (ARGS="--force" | "--no-embeddings")
	$(VENV)/python -m ken_services.intelligence.cli analyze $(ARGS)

recommend: ## Phase 3: propose contextual links for eligible sources (ARGS="--source PAGE_ID ...")
	$(VENV)/python -m ken_services.recommendations.cli recommend $(ARGS)

# A sleeping Mac suspends the process (the shadow pilot's 2-hour 'critic' time was sleep): hold the
# machine awake for long local jobs where caffeinate exists. `-i` alone did NOT stop a later
# sleep during the model comparison; `-dims` did.
KEEP_AWAKE := $(shell command -v caffeinate >/dev/null 2>&1 && echo caffeinate -dims)

agents: ## Phase 4: agents review proposed plans and hand accepted ones on to the autonomy engine (ARGS="--source PAGE_ID ...")
	$(KEEP_AWAKE) $(VENV)/python -m ken_agents.cli agents $(ARGS)

# ---------------------------------------------------------------- users (Phase 5)
# Accounts are created ONLY here (no HTTP endpoint, no default credentials). Passwords are
# prompted for (or read from stdin with PASSWORD_STDIN=1), never passed as arguments.
user-create: ## Create a user: make user-create USERNAME=… ROLE=viewer|reviewer|operator_admin DISPLAY_NAME="…"
	@test -n "$(USERNAME)" && test -n "$(ROLE)" || (echo 'usage: make user-create USERNAME=… ROLE=… DISPLAY_NAME="…" [PASSWORD_STDIN=1]' >&2; exit 2)
	$(VENV)/python -m ken_services.auth.cli user-create --username "$(USERNAME)" --role "$(ROLE)" --display-name "$(or $(DISPLAY_NAME),$(USERNAME))" $(if $(PASSWORD_STDIN),--password-stdin)

user-list: ## List users (never password hashes)
	$(VENV)/python -m ken_services.auth.cli user-list

user-disable: ## Disable a user and revoke their sessions: make user-disable USERNAME=…
	@test -n "$(USERNAME)" || (echo "usage: make user-disable USERNAME=…" >&2; exit 2)
	$(VENV)/python -m ken_services.auth.cli user-disable --username "$(USERNAME)"

user-enable: ## Re-enable a user and clear a lockout: make user-enable USERNAME=…
	@test -n "$(USERNAME)" || (echo "usage: make user-enable USERNAME=…" >&2; exit 2)
	$(VENV)/python -m ken_services.auth.cli user-enable --username "$(USERNAME)"

user-set-role: ## Change a role and revoke sessions: make user-set-role USERNAME=… ROLE=…
	@test -n "$(USERNAME)" && test -n "$(ROLE)" || (echo "usage: make user-set-role USERNAME=… ROLE=…" >&2; exit 2)
	$(VENV)/python -m ken_services.auth.cli user-set-role --username "$(USERNAME)" --role "$(ROLE)"

user-set-password: ## Set a password and revoke sessions: make user-set-password USERNAME=…
	@test -n "$(USERNAME)" || (echo "usage: make user-set-password USERNAME=… [PASSWORD_STDIN=1]" >&2; exit 2)
	$(VENV)/python -m ken_services.auth.cli user-set-password --username "$(USERNAME)" $(if $(PASSWORD_STDIN),--password-stdin)

sessions-prune: ## Delete sessions that expired more than 30 days ago
	$(VENV)/python -m ken_services.auth.cli sessions-prune

crawl: ## Crawl the ONE site configured in KEN_CRAWL_BASE_URL (refuses if unset)
	$(VENV)/python -m ken_services.inventory.cli crawl

# ---------------------------------------------------------------- deployment (Phase 6)
# Operator commands only: nothing here deploys. The simulated CMS is a disposable,
# project-local database (KEN_CMS_SIM_DATABASE_URL, default ken_cms_sim on :5433).
deployments-reconcile: ## Resend stuck applying/rolling_back deployments (ARGS="--older-than-s N")
	$(VENV)/python -m ken_services.deployment.cli reconcile $(ARGS)

jobs-reap: ## Fail 'running' job runs whose kind lock no live runner holds (never deployments)
	$(VENV)/python -m ken_services.jobs.reaper $(ARGS)

cms-sim-seed: ## Create + seed the simulated CMS from the inventory (ARGS="--export FILE --reset")
	$(VENV)/python -m ken_services.deployment.cms_sim seed $(ARGS)

cms-sim-serve: ## Serve the simulated CMS on 127.0.0.1:<free port> (prints the verified URL)
	$(VENV)/python -m ken_services.deployment.cms_sim serve

# ---------------------------------------------------------------- analytics (Phase 7)
# Operator commands only (ADR-0015 section 6): there is NO HTTP route that imports, so no UI
# action can cause Google egress. GA4 / Search Console calls need KEN_ANALYTICS_GOOGLE_ENABLED
# (ASK) and a 0600 token file; without FILE= and without that setting the import is refused
# before any I/O. Every run is one ANALYTICS_IMPORT job, so `make jobs-reap` applies.
analytics-import: ## Import daily rows: make analytics-import SOURCE=ga4|gsc|first_party DATASET=links|pages START=YYYY-MM-DD END=YYYY-MM-DD [FILE=rows.csv] [ARGS=--replace-via]
	@test -n "$(SOURCE)" && test -n "$(DATASET)" && test -n "$(START)" && test -n "$(END)" \
	  || (echo 'usage: make analytics-import SOURCE=ga4|gsc|first_party DATASET=links|pages START=YYYY-MM-DD END=YYYY-MM-DD [FILE=rows.csv]' >&2; exit 2)
	$(VENV)/python -m ken_services.analytics.cli import --source "$(SOURCE)" --dataset "$(DATASET)" --start "$(START)" --end "$(END)" $(if $(FILE),--file "$(FILE)") $(ARGS)

analytics-aggregate: ## First-party link events -> daily rows, then the measure sweep: make analytics-aggregate [START= END=]
	$(VENV)/python -m ken_services.analytics.cli aggregate $(if $(START),--start "$(START)") $(if $(END),--end "$(END)")

analytics-demo-seed: ## Seeded demo analytics (local/ci only, never mixed with real rows): make analytics-demo-seed ARGS="--seed 7"
	$(VENV)/python -m ken_services.analytics.cli demo-seed $(ARGS)

analytics-sim-serve: ## Serve the simulated CMS with the beacon + the first-party collector (two verified loopback ports)
	$(VENV)/python -m ken_services.analytics.collector.serve

# ---------------------------------------------------------------- compose
compose-config: ## Validate docker-compose.yml in all three modes
	$(COMPOSE) config --quiet && echo "compose: default (Mode A) OK"
	$(COMPOSE) --profile full config --quiet && echo "compose: full (Mode B) OK"
	$(COMPOSE) --profile full --profile ollama-container config --quiet && echo "compose: full + ollama-container OK"

compose-full-up: ## Mode B: full container stack (api, worker, web, postgres)
	$(COMPOSE) --profile full up -d --build

compose-full-down: ## Stop Mode B app containers; Postgres keeps running for Mode A
	$(COMPOSE) --profile full stop api worker web

# ----------------------------------------------------------------- learning (Phase 8)
# CLI-ONLY, deliberately (ADR-0016 section 10): no UI button starts a model run, writes a
# policy file or triggers egress. The API has GET routes plus ONE write route that records a
# stage change. These targets resolve `ken_services.learning.cli`, which the Phase 8
# workstreams land; until then they fail with a module-not-found, like the staged readers.
#
# There is deliberately NO experiment-start / experiment-end target. `policy_ab` and `holdout`
# are both built (ADR-0016 17.2), but a holdout withholds inbound links from live pages, so
# activating one is gated three ways (a 0.20 DB cap, Settings.experiments_allow_holdout =
# false by default, and a transaction-local activation setting). Activation and ending are
# reachable only as `python -m ken_services.learning.cli experiment-activate|experiment-end
# --key K --as-user NAME`, never as a convenience target.
reflect: ## Corpus-level reflection -> learning proposals: make reflect [START= END= ARGS=--no-model]
	$(VENV)/python -m ken_services.learning.cli reflect $(if $(START),--start "$(START)") $(if $(END),--end "$(END)") $(ARGS)

eval: ## Offline evaluation: make eval SUITES=critic,no-action [PROPOSAL=<uuid>] [SKILL=<uuid>]
	@test -n "$(SUITES)" || (echo 'usage: make eval SUITES=<comma-separated> [PROPOSAL=<uuid>] [SKILL=<uuid>]' >&2; exit 2)
	$(VENV)/python -m ken_services.learning.cli eval --suites "$(SUITES)" $(if $(PROPOSAL),--proposal "$(PROPOSAL)") $(if $(SKILL),--skill "$(SKILL)") $(ARGS)

shadow: ## Re-run selection with a candidate beside production, writing no plans: make shadow CANDIDATE=<uuid>
	@test -n "$(CANDIDATE)" || (echo 'usage: make shadow CANDIDATE=<uuid>' >&2; exit 2)
	$(VENV)/python -m ken_services.learning.cli shadow --candidate "$(CANDIDATE)" $(ARGS)

policy-promote: ## Write the promoted policy file(s). Refuses unless a human `promoted` transition with an eval ref exists.
	@test -n "$(PROPOSAL)" || (echo 'usage: make policy-promote PROPOSAL=<uuid> [ARGS=--apply]' >&2; exit 2)
	$(VENV)/python -m ken_services.learning.cli policy-promote --proposal "$(PROPOSAL)" $(ARGS)

experiment-create: ## Create a DRAFT experiment: make experiment-create KEY=<key> CANDIDATE=<uuid> AS_USER=<name> [KIND=policy_ab|holdout FRACTION=0.5]
	@test -n "$(KEY)" -a -n "$(AS_USER)" || (echo 'usage: make experiment-create KEY=<key> AS_USER=<operator> [CANDIDATE=<uuid>] [KIND=policy_ab|holdout] [FRACTION=0.5]' >&2; exit 2)
	$(VENV)/python -m ken_services.learning.cli experiment-create --key "$(KEY)" --as-user "$(AS_USER)" $(if $(KIND),--kind "$(KIND)") $(if $(CANDIDATE),--candidate "$(CANDIDATE)") $(if $(FRACTION),--fraction "$(FRACTION)") $(ARGS)

experiment-assign: ## Materialise the deterministic arm per source page (draft experiments only)
	@test -n "$(KEY)" || (echo 'usage: make experiment-assign KEY=<key>' >&2; exit 2)
	$(VENV)/python -m ken_services.learning.cli experiment-assign --key "$(KEY)" $(ARGS)

# ---------------------------------------------------------------- autonomy (Phase 9)
# CLI-ONLY (ADR-0018 section 5): no UI button starts an evaluation pass or a publish sweep.
# The API has GET routes plus ONE operator-override write route.
#
# What actually happens depends on KEN_AUTONOMY_MODE, whose default is `shadow`: `make
# autonomy` in shadow mode records decisions and publishes nothing. Every pass is one
# `autonomy` / `autonomy_shadow` job run, so `make jobs-reap` applies to a killed pass.
#
# A PUBLISHING pass needs AS_USER=<username> (the same spelling `make experiment-create`
# uses; never USER, which every login shell already exports): `deployments.actor` carries a
# `^user:` CHECK and `user_id` is a NOT NULL foreign key to `users`, so a CMS write must be
# attributable to a real operator account with the `deploy` permission (`make user-create`).
# That is the audit identity for the run, NOT an approval: nobody is asked about any
# individual link.
autonomy: ## Evaluate eligible plans under KEN_AUTONOMY_MODE (ARGS="--source PAGE_ID ...")
	$(VENV)/python -m ken_services.autonomy.cli evaluate $(ARGS)

autonomy-shadow: ## Force a shadow pass: record what WOULD happen, publish nothing
	$(VENV)/python -m ken_services.autonomy.cli evaluate --mode shadow $(ARGS)

autonomy-run: ## Publish+verify eligible plans as an operator: make autonomy-run AS_USER=…
	@test -n "$(AS_USER)" || (echo 'usage: make autonomy-run AS_USER=<username> [ARGS="--source PAGE_ID ..."]' >&2; exit 2)
	$(VENV)/python -m ken_services.autonomy.cli evaluate --as-user "$(AS_USER)" $(ARGS)

autonomy-legacy: ## Classify plans stranded in awaiting_approval (dry run; ARGS=--apply expires/blocks the invalid ones)
	$(VENV)/python -m ken_services.autonomy.cli legacy-reconcile $(ARGS)

autonomy-dry-run: ## Evaluate and print; record nothing (no decisions, transitions, attempts)
	$(VENV)/python -m ken_services.autonomy.cli evaluate --dry-run $(ARGS)

# ---------------------------------------------------------------- doctor / gate
doctor: ## Print the environment matrix; non-zero exit only on hard failures
	$(VENV)/python infrastructure/scripts/doctor.py

verify: lint types test-all web-test compose-config migrate doctor ## The Phase gate
	@echo ""; echo "verify: all gates passed"

# ----------------------------------------------------------------- demo
# A repeatable, fully local, end-to-end demonstration (docs/DEMO.md). It recreates its OWN
# databases (ken_demo, ken_cms_sim_demo on :5433) and Qdrant collections (ken_demo_*), uses the
# deterministic mock model and a simulated CMS, and refuses to run against anything else.
# Needs `make db-up qdrant-up` (or `make dev`). Takes about a minute.
# KEN_DEMO_ADMIN_PASSWORD=... make demo   also creates a demo_admin login for browsing the UI.
demo: ## Run the local end-to-end demo (about a minute; docs/DEMO.md). ARGS=--ollama is opt-in
	$(VENV)/python infrastructure/scripts/demo.py $(ARGS)

demo-clean: ## Drop the demo databases (ken_demo, ken_cms_sim_demo) and delete ken_demo_* collections
	$(VENV)/python infrastructure/scripts/demo.py clean

# ----------------------------------------------------------- database roles (ADR-0014 s5)
# The application connects as the least-privilege role `ken_app`; migrations and the dev tools
# that create databases use the owner `ken`. See docs/DEVELOPMENT.md "Database roles".
# Both refuse port 5432 and any database not named ken_*. The password is generated at runtime
# (or taken from KEN_APP_DB_PASSWORD) and is never printed.
db-roles: ## Create/update the ken_app role and grants. ARGS=--write-env appends the DSNs to .env
	$(VENV)/python infrastructure/scripts/db_roles.py apply $(ARGS)

# ---------------------------------------------------- Ken public crawl pilot (docs/pilot/KEN_PUBLIC_CRAWL_PILOT.md)
# NONE of these sends a request to the target site except pilot-robots / pilot-crawl, which require an
# approval file (pilots/kenresearch/approvals/, gitignored) quoting the exact configuration hash.
PILOT ?= pilots/kenresearch/pilot-25.yaml

pilot-db-create: ## Local only: create + migrate + grant the SEPARATE pilot database (never the demo ones)
	$(VENV)/python infrastructure/scripts/pilot_db.py --config $(PILOT)

pilot-preflight: ## OFFLINE report of what the pilot would do + blockers (sends nothing to the site)
	$(VENV)/python -m ken_services.inventory.pilot preflight --config $(PILOT)

pilot-robots: ## LIVE, approval-gated: fetch ONLY robots.txt of the approved hosts. make pilot-robots APPROVAL=file
	@test -n "$(APPROVAL)" || (echo 'usage: make pilot-robots APPROVAL=pilots/kenresearch/approvals/<file>.json' >&2; exit 2)
	$(VENV)/python -m ken_services.inventory.pilot preflight --config $(PILOT) --live-robots --approval $(APPROVAL)

pilot-crawl: ## LIVE, approval-gated crawl. NOT run without explicit authorisation. make pilot-crawl APPROVAL=file
	@test -n "$(APPROVAL)" || (echo 'usage: make pilot-crawl APPROVAL=pilots/kenresearch/approvals/<file>.json' >&2; exit 2)
	$(KEEP_AWAKE) $(VENV)/python -m ken_services.inventory.pilot crawl --config $(PILOT) --approval $(APPROVAL)

# ----------------------------------------------- offline interlinking demo (docs/pilot/OFFLINE_INTERLINKING_DEMO.md)
# Synthetic corpus only (plus any folder YOU are already authorised to use). Shadow mode, mock CMS, no website.
offline-demo: ## Full local shadow pipeline + management report (real local Ollama critic, ~12 min). ARGS=--fresh
	$(VENV)/python evals/offline_demo/run_demo.py $(ARGS)

offline-demo-fast: ## Same pipeline with the deterministic rule critic (~2 min)
	$(VENV)/python evals/offline_demo/run_demo.py --fresh --rule-critic

offline-demo-report: ## Rebuild only the report from the demo database
	KEN_DATABASE_URL=$$($(VENV)/python -c "import sys; sys.path.insert(0,'evals/offline_demo'); import run_demo; print(run_demo.environment(False)['KEN_DATABASE_URL'])") $(VENV)/python evals/offline_demo/report.py

import-html-dir: ## Import a local HTML folder you may use: make import-html-dir DIR=... SITE=https://host/ PROVENANCE=authorized_import
	@test -n "$(DIR)" -a -n "$(SITE)" || (echo 'usage: make import-html-dir DIR=<folder> SITE=<url> [PROVENANCE=authorized_import]' >&2; exit 2)
	$(VENV)/python -m ken_services.inventory.cli import-html-dir "$(DIR)" --site-url "$(SITE)" $(if $(PROVENANCE),--provenance $(PROVENANCE),)

db-roles-check: ## Audit the live grants; non-zero if ken_app is a superuser, owns anything or can TRUNCATE/DDL
	$(VENV)/python infrastructure/scripts/db_roles.py check

db-roles-test: ## Privilege tests AS ken_app (needs KEN_TEST_APP_DATABASE_URL + KEN_TEST_DATABASE_URL)
	$(VENV)/pytest -q tests/integration/test_app_role.py
