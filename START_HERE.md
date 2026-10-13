# Ken Research Agentic Interlinking OS — Start Here

This folder is the bootstrap package for building the Ken Research Agentic Interlinking OS with Claude Code in VS Code.

## Objective

Build a fully self-hosted, open-source-first, standalone application that behaves like an autonomous AI employee for contextual body interlinking.

The system must:

1. ingest and understand Ken Research pages;
2. build a semantic/content graph;
3. identify link opportunities;
4. generate explainable link plans;
5. request manual approval during the learning phase;
6. publish approved links through a controlled CMS API;
7. verify deployment;
8. ingest analytics;
9. learn from human feedback and outcomes;
10. progressively earn autonomy for proven decision classes.

## Hard Constraints

- No mandatory paid LLM APIs.
- Local/self-hosted inference only by default.
- Ollama is the initial inference server.
- Qwen-family models are the preferred first reasoning candidates.
- PostgreSQL + pgvector is the initial system of record and vector store.
- LangGraph is the initial supervisor/state-machine framework.
- Agents never write directly to production data stores.
- Production mutations occur only through explicit validated APIs.
- Every mutation must be versioned, idempotent, auditable, and reversible.
- Human approval is mandatory during the learning phase.
- Policy changes are proposed by agents but never self-promoted.
- Prefer deterministic code over LLM reasoning where deterministic logic is sufficient.

## Read Order for Claude Code

Read these files in this exact order before modifying code:

1. `START_HERE.md`
2. `PRD.md`
3. `TRD.md`
4. `CLAUDE.md`
5. `AUTONOMY_AND_PERMISSIONS.md`
6. `REPO_CLONE_PLAN.md`
7. `IMPLEMENTATION_PLAN.md`
8. `INITIAL_CLAUDE_CODE_PROMPT.md`

## Development Mode

Build one phase at a time. Do not attempt to build the whole system in one pass.

Each phase must end with tests, lint/type checks, updated docs, a clean migration state, a concise implementation summary, and an explicit unresolved-issues list.

## First Action

Open this folder as the VS Code workspace and paste the complete content of `INITIAL_CLAUDE_CODE_PROMPT.md` into Claude Code.

Do not clone all reference repositories into the production source tree. Clone them into `_research/repos/` only for inspection, unless the TRD explicitly approves a dependency.
