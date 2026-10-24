# Implementation Plan

## Phase 0 — Foundation
Deliver monorepo, FastAPI, Next.js, PostgreSQL (Qdrant added by ADR-0017; pgvector superseded), Ollama provider abstraction, Docker Compose, SQLAlchemy/Alembic, baseline contracts, tests/lint/type checks and a mock CMS.

Exit: `docker compose up` starts the local stack and tests pass.

## Phase 1 — Content Inventory
Crawler, parser, canonical page model, content hashing, existing-link extraction and page/version persistence.

## Phase 2 — Content Intelligence
Taxonomy, entity extraction, embeddings, vectors (Qdrant per ADR-0017) and graph relationships.

## Phase 3 — Recommendation Engine
Hybrid retrieval, eligibility filters, scoring, contextual placement, LinkPlan and explanation.

## Phase 4 — Agent Orchestration
LangGraph supervisor plus triage, discovery, planner and critic nodes with typed state/failures.

## Phase 5 — Human Approval UI
Inbox, detail view, approve/reject/edit, reason codes and structured feedback persistence.

## Phase 6 — Publishing and Verification
CMS adapter, mock CMS E2E, dry run, optimistic locking, idempotency, rollback, HTML verification and optional Playwright verification.

## Phase 7 — Analytics
Link IDs, first-party events, GA4/GSC provider interfaces, CSV fallback and dashboards.

## Phase 8 — Learning
Evaluation datasets, feedback analysis, skill registry, skill proposals, policy proposals and offline evals.

## Phase 9 — Selective Autonomy
Decision classes, calibration, historical approval metrics, and BLOCK / REQUIRE_APPROVAL / AUTO_APPROVE policy engine.

AUTO_APPROVE stays disabled until explicitly authorized.

## Phase Boundary Rule

At every phase create `PHASE_<N>_REPORT.md`, update `NEXT_STEPS.md`, run tests/checks and stop before the next phase unless explicitly authorized.
