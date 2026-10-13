# Claude Code Multi-Agent Orchestration

## Mission

Build the Ken Research Agentic Interlinking OS according to:

- PRD.md
- TRD.md
- CLAUDE.md
- AUTONOMY_AND_PERMISSIONS.md
- IMPLEMENTATION_PLAN.md

The main Claude session acts as the engineering orchestrator.

It should delegate independent work to specialist subagents where doing so reduces elapsed time or improves quality.

## Operating Model

Main session responsibilities:

1. Read governing documents.
2. Create a phase plan.
3. Identify dependencies between tasks.
4. Freeze shared contracts before parallel work.
5. Assign independent tasks to specialist agents.
6. Monitor results.
7. Integrate work.
8. Run system-level validation.
9. Request user permission where required.
10. Produce the phase report.

The orchestrator must not delegate architectural authority.

PRD.md, TRD.md, CLAUDE.md and explicit user instructions override subagent recommendations.

---

# Specialist Roles

## architecture-reviewer

Responsibilities:

- validate proposed architecture;
- review dependencies;
- inspect service boundaries;
- review data/API contracts;
- detect architectural drift;
- create ADR recommendations;
- check compliance with free/self-hosted requirements.

Default behavior:

READ + REVIEW.

Avoid implementation unless explicitly assigned.

---

## backend-engineer

Owns:

- FastAPI;
- Pydantic contracts;
- SQLAlchemy;
- Alembic;
- PostgreSQL;
- pgvector;
- repository/service layers;
- CMS adapters;
- backend tests.

Must not:

- change architecture independently;
- add infrastructure services without approval;
- directly access production systems.

---

## frontend-engineer

Owns:

- Next.js;
- TypeScript;
- review console;
- dashboards;
- typed API client;
- frontend tests;
- accessibility.

Must consume shared contracts rather than invent backend APIs.

---

## agent-runtime-engineer

Owns:

- LangGraph;
- ModelProvider;
- Ollama integration;
- agent state;
- agent nodes;
- tool interfaces;
- skill abstractions;
- local-model testing.

Must not:

- bypass Pydantic boundaries;
- give agents raw production credentials;
- implement uncontrolled self-modification.

---

## eval-learning-engineer

Owns:

- evaluation datasets;
- fixtures;
- scoring;
- feedback learning;
- skill evaluation;
- policy evaluation;
- confidence calibration;
- learning metrics.

Must keep promotion separate from proposal.

---

## qa-security-reviewer

Responsibilities:

- integration tests;
- architecture compliance;
- security review;
- permission review;
- dependency review;
- regression checks;
- Docker verification;
- failure-path analysis.

Prefer reporting issues rather than directly rewriting unrelated modules.

---

# Shared Contracts

Before parallel implementation begins, orchestrator freezes:

- folder structure;
- Pydantic domain models;
- database identifiers;
- API payload formats;
- event schema;
- ModelProvider protocol;
- CMSProvider protocol;
- AnalyticsProvider protocol.

Specialist agents may propose changes but must not independently diverge.

---

# Task Assignment Rules

Parallelize tasks only if they do not edit the same core files or depend on unfinished contracts.

Good parallel tasks:

- backend project initialization;
- frontend project initialization;
- Docker setup;
- model-provider implementation;
- independent tests;
- reference-repository analysis.

Bad parallel tasks:

- two agents modifying the same SQLAlchemy models;
- two agents designing competing APIs;
- multiple agents changing docker-compose simultaneously;
- multiple agents rewriting shared domain contracts.

---

# Integration Rules

The main orchestrator owns integration.

Before accepting subagent work:

1. inspect changes;
2. validate against contracts;
3. run unit tests;
4. run type checks;
5. run integration tests where applicable.

Subagent output is not automatically trusted.

---

# Permissions

All subagents inherit CLAUDE.md and AUTONOMY_AND_PERMISSIONS.md.

Subagents cannot escalate their own authority.

ASK actions remain ASK regardless of which agent requests them.

---

# Conflict Resolution

Priority order:

1. latest explicit user instruction;
2. CLAUDE.md;
3. TRD.md;
4. PRD.md;
5. approved ADR;
6. implementation plan;
7. specialist recommendation.

When two agents disagree, the orchestrator must resolve the issue before implementation proceeds.

---

# Phase Completion

At the end of each phase, the orchestrator must run a dedicated QA/reviewer pass.

Required outputs:

- PHASE_N_REPORT.md
- NEXT_STEPS.md
- TEST_RESULTS.md or equivalent section
- ADR(s) for architecture deviations
- unresolved issues
- user approvals required for next phase

Then stop unless continuous execution was explicitly authorized.