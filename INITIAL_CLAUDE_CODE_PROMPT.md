# Initial Claude Code Prompt

Paste everything below into Claude Code after opening this folder in VS Code.

---

You are the lead engineer for the Ken Research Agentic Interlinking OS.

Before you write or modify any code, read these files completely and in this order:

1. START_HERE.md
2. PRD.md
3. TRD.md
4. CLAUDE.md
5. AUTONOMY_AND_PERMISSIONS.md
6. REPO_CLONE_PLAN.md
7. IMPLEMENTATION_PLAN.md

Treat them as the source of truth.

Your task right now is to execute **Phase 0 only**.

## Phase 0 Objective

Create the standalone local-development foundation for the Ken Research Agentic Interlinking OS.

Build:
- monorepo structure defined in TRD.md;
- FastAPI backend;
- Next.js + TypeScript frontend;
- PostgreSQL;
- pgvector;
- SQLAlchemy;
- Alembic;
- Pydantic contracts;
- Ollama model-provider abstraction;
- mock CMS API/service;
- Docker Compose;
- `.env.example`;
- backend pytest setup;
- frontend lint/type/test setup;
- health endpoints;
- database readiness/health checks;
- basic structured logging;
- a minimal local developer README.

Do NOT implement interlink recommendation intelligence yet.

Do NOT add paid APIs.

Do NOT add Qdrant, Neo4j, Kafka, Redis, Celery, Temporal or any other infrastructure service unless Phase 0 becomes impossible without it. If you believe one is required, stop and ask first with a clear justification.

## Reference Repository Research

Create `_research/repos/` and ensure it is ignored by Git.

If not already available locally, you may clone these repositories into `_research/repos/` without asking again:
- https://github.com/NousResearch/hermes-agent
- https://github.com/FoundationAgents/OpenManus
- https://github.com/langchain-ai/langgraph
- https://github.com/huggingface/smolagents
- https://github.com/camel-ai/owl
- https://github.com/ollama/ollama
- https://github.com/QwenLM/Qwen3

These are research references. Do not copy code into production without checking the repository license and documenting provenance.

For Phase 0, inspect only what is necessary to understand local model-provider patterns, agent state/runtime patterns, project structure and permissions/skills concepts.

Do not rewrite the project around any one reference repository.

## Permission Behavior

Work autonomously on ordinary local development actions.

Do not ask permission before:
- creating/editing local files;
- running tests;
- running lint/type checks;
- creating migrations;
- adding dependencies already approved in TRD.md;
- running local Docker Compose;
- cloning the approved reference repositories into `_research/repos/`.

You MUST ask before:
- pushing to a remote Git repository;
- creating/merging a PR;
- touching a real Ken CMS;
- using real CMS write credentials;
- destructive database operations;
- enabling automatic publishing;
- introducing a paid API;
- materially changing the architecture;
- adding a major infrastructure dependency not in TRD.md;
- sending source/data to an external service.

## Architecture Rules

- Ollama must sit behind a `ModelProvider` interface.
- No business logic may call Ollama directly.
- PostgreSQL is the source of truth.
- pgvector is the initial vector layer.
- All future model outputs crossing a service boundary must be Pydantic validated.
- Agents will never directly mutate production CMS storage.
- Production mutations must eventually pass through a typed CMS adapter.
- Prefer deterministic code over agent reasoning.
- Preserve auditability.

## Required Phase 0 Artifacts

At minimum create:
- `README.md`
- `.env.example`
- `.gitignore`
- `docker-compose.yml`
- backend application
- frontend application
- DB configuration
- migration configuration
- `ModelProvider` protocol/interface
- `OllamaProvider`
- mock model provider for tests
- base shared/domain models
- health endpoint
- mock CMS service or adapter
- test suite
- `NEXT_STEPS.md`
- `PHASE_0_REPORT.md`

## Validation Before Completion

Run:
1. backend tests;
2. backend lint/type checks;
3. frontend lint/type/test checks;
4. Docker Compose config validation;
5. application health checks if local runtime permits.

Fix failures within Phase 0 scope.

## End-of-Phase Report

When Phase 0 is complete:
- summarize architecture created;
- list dependencies added;
- list tests run and results;
- list deviations from TRD.md;
- list unresolved local-environment issues;
- update NEXT_STEPS.md for Phase 1.

Then STOP.

Do not start Phase 1 until I explicitly tell you to proceed.

---
