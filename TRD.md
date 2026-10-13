# Ken Research Agentic Interlinking OS
## Technical Requirements Document — v1.0

## 1. Technical Objective

Build a standalone self-hosted application that performs:

crawl → understand → retrieve → reason → recommend → approve → publish → verify → measure → learn

without mandatory paid APIs.

## 2. Initial Technology Stack

### Frontend
- Next.js
- TypeScript
- Tailwind CSS

### Backend
- Python
- FastAPI
- Pydantic
- SQLAlchemy
- Alembic

### Agent Runtime
- LangGraph for the supervisor/state machine.
- smolagents may be used only for targeted specialist experiments after comparison.
- Do not stack multiple agent frameworks without an Architecture Decision Record.

### Local Inference
- Ollama.
- Model names supplied through configuration, not hardcoded.
- Preferred first family: Qwen.
- Optional later llama.cpp provider.

### Data
- PostgreSQL.
- pgvector.
- No Qdrant/Neo4j in v1 unless a measured requirement justifies them.

### Crawling / Parsing
- httpx.
- BeautifulSoup.
- Scrapy where scale warrants it.

### Browser Verification
- Playwright first.
- Browser Use may be evaluated for ambiguous agentic browser tasks.

### Observability
- Structured application logs required.
- OpenTelemetry supported.
- Self-hosted Langfuse optional.

## 3. Core Architecture

UI → FastAPI Control API → LangGraph Supervisor → Specialist Agent Nodes / Deterministic Services → Local Model Provider → PostgreSQL/pgvector → CMS API → Production → Analytics Import → Learning/Evaluation

## 4. Repository Layout

Use a monorepo with apps/web, apps/api, agents/*, services/*, tools/*, skills/*, workflows, contracts, evals, policies, migrations, tests and infrastructure.

## 5. Required Domain Tables

Minimum:
- pages
- page_versions
- page_entities
- page_embeddings
- graph_edges
- existing_links
- opportunities
- link_candidates
- link_plans
- link_reviews
- link_deployments
- link_events
- experiments
- experiment_assignments
- agent_runs
- skills
- policies
- policy_evaluations
- events

All mutable state requires created_at/updated_at as appropriate.

## 6. LLM Boundary Rule

Every output from a model that crosses into application logic must be validated through a Pydantic model.

No raw free-form model output may trigger database mutation, publication, rollback, policy change or permission change.

## 7. Model Provider Interface

Create an abstract provider interface.

Initial implementation: OllamaProvider.
Future: LlamaCppProvider.

Agents cannot call Ollama directly.

Configuration:
- MODEL_FAST
- MODEL_REASONING
- MODEL_EMBEDDING

## 8. Agent Supervisor State

Primary state includes task_id, source_page_id, trigger, source_page, classification, candidates, ranked_candidates, link_plans, critic_result, risk_level, approval_required, approval_result, deployment_result, verification_result and errors.

## 9. State Machine

START → LOAD_PAGE → TRIAGE → UNDERSTAND → RETRIEVE → FILTER → RANK → PLACE → CRITIQUE → POLICY_GATE → HUMAN_APPROVAL or AUTO_APPROVE → VALIDATE → PUBLISH → VERIFY → MEASURE → END

Any node may return a typed recoverable error.

## 10. Agent Responsibilities

### Triage
Returns NO_ACTION, REINDEX, OPTIMIZE, REPAIR_LINK or REVIEW_PERFORMANCE.

### Understanding
Extracts structured domain classification.

### Discovery
Uses deterministic retrieval tools and returns candidate identifiers/features.

### Planner
Produces LinkPlan objects.

### Critic
Evaluates plan quality but cannot publish.

### Verifier
Confirms production state.

### Learning
Produces skill/policy proposals only.

## 11. Deterministic Services

Do not delegate URL normalization, status checks, content hashing, duplicate detection, canonical validation, protected-page checks, source version checks, HTML mutation, link existence verification, link density limits, publishing, rollback or metrics aggregation to an LLM.

## 12. Initial Ranking Policy

Configurable baseline:
- semantic 0.30
- taxonomy 0.20
- intent 0.15
- graph 0.15
- geography 0.08
- commercial 0.05
- underlinked 0.04
- historical 0.03

Weights live in versioned policy data, not source-code constants.

## 13. CMS Contract

Required conceptual endpoints:
- GET /internal/pages/{id}
- POST /internal/pages/{id}/link-changes/validate
- POST /internal/pages/{id}/link-changes
- POST /internal/deployments/{id}/rollback

Mutation requests must include operation_id, expected_version, source_page_id and changes.

Version conflict returns 409 and forces regeneration.

## 14. Approval API

- GET /api/recommendations
- GET /api/recommendations/{id}
- POST /api/recommendations/{id}/approve
- POST /api/recommendations/{id}/reject
- PATCH /api/recommendations/{id}

No production mutation before required approval.

## 15. Events

Initial event bus may be PostgreSQL-backed.

Event names include page.created, page.updated, page.deleted, crawl.completed, link.proposed, link.approved, link.rejected, link.deployed, link.verified, link.failed, link.rolled_back, analytics.updated, skill.proposed, policy.proposed and policy.promoted.

## 16. Approval Suspension

Do not leave a live process waiting for a human. Persist task state as AWAITING_APPROVAL and resume from an approval event.

## 17. Skill Registry

Each skill has name, version, description, instructions, input schema, output schema, source, evaluation score and status. Skill revisions create a new version.

## 18. Learning Safety

Allowed:
experience → reflection → proposal → evaluation → activation

Forbidden:
experience → immediate self-modification in production

## 19. Evals

Minimum suites:
- destination-selection
- anchor-selection
- relationship-detection
- critic
- no-action
- protected-content
- stale-page-version

## 20. Autonomy Manager

Track per decision class sample_count, approval_rate, edit_rate, rollback_rate, positive_outcome_rate, calibrated confidence and autonomy_level.

Policy decisions:
- BLOCK
- REQUIRE_APPROVAL
- AUTO_APPROVE

AUTO_APPROVE must be disabled globally in v1.

## 21. Docker Compose

Initial services:
- postgres
- ollama
- api
- worker
- web

Optional:
- langfuse and its required self-hosted supporting services.

## 22. Testing

### Unit
Ranking, eligibility, policies, placement, mutation generation.

### Agent
Recorded fixtures for candidate selection, critic and no-action.

### Integration
crawl→DB, page→embedding, candidate→plan, approval→deployment, deployment→verification.

### E2E
Use a mock CMS before live CMS integration.

## 23. Security

- secrets via env/secret store;
- RBAC;
- least-privilege CMS credentials;
- no arbitrary model-generated shell commands in production;
- tool allowlists;
- audited mutations;
- protected page rules;
- isolated browser execution where possible.

## 24. Architecture Rule

Probabilistic reasoning must always terminate at a typed deterministic boundary before any irreversible action.

LLM → structured decision → policy → deterministic validation → approval/authorization → deterministic execution → measurement → learning
