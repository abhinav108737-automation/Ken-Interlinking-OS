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
.PHONY: help setup lock db-up db-down migrate dev dev-api dev-web \
        test test-integration test-all test-live web-test lint format types \
        compose-config compose-full-up compose-full-down doctor verify

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

migrate: ## Apply Alembic migrations to KEN_DATABASE_URL
	$(VENV)/alembic upgrade head

# ---------------------------------------------------------------- run
dev: db-up migrate ## Start Postgres + migrate, then run dev-api and dev-web in two terminals
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

test-all: ## Everything except live tests; FAILS if the test DB is unreachable
	KEN_REQUIRE_INTEGRATION_DB=1 $(VENV)/pytest -m "not live"

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

# ---------------------------------------------------------------- compose
compose-config: ## Validate docker-compose.yml in all three modes
	$(COMPOSE) config --quiet && echo "compose: default (Mode A) OK"
	$(COMPOSE) --profile full config --quiet && echo "compose: full (Mode B) OK"
	$(COMPOSE) --profile full --profile ollama-container config --quiet && echo "compose: full + ollama-container OK"

compose-full-up: ## Mode B: full container stack (api, worker, web, postgres)
	$(COMPOSE) --profile full up -d --build

compose-full-down: ## Stop Mode B app containers; Postgres keeps running for Mode A
	$(COMPOSE) --profile full stop api worker web

# ---------------------------------------------------------------- doctor / gate
doctor: ## Print the environment matrix; non-zero exit only on hard failures
	$(VENV)/python infrastructure/scripts/doctor.py

verify: lint types test-all web-test compose-config migrate doctor ## The Phase gate
	@echo ""; echo "verify: all gates passed"
