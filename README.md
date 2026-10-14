# Ken Research Agentic Interlinking OS

A fully self-hosted, local-first system that will manage contextual internal body links across Ken Research
pages: observe → understand → recommend → approve → publish → verify → measure → learn.

**Current status: Phase 2 — content intelligence complete** (Phase 1 inventory beneath it).

Phase 2 adds deterministic understanding of every page from a versioned taxonomy policy:
- page type, intent, market, geography, entities, keywords and an extractive summary;
- local embeddings (`nomic-embed-text` via Ollama) in pgvector;
- typed page relationships;
- protected-page flags;
- link eligibility.

Its inspection screens are `/intelligence` and `/jobs`.

Phase 1 can ingest pages from:
- a polite, robots-respecting crawler, tested only against local fixtures, with **no site configured
  yet**;
- a JSONL export;
- a read-only CMS adapter (mock only).

It normalises pages deterministically, stores them with an immutable, hash-based version history,
extracts the links they already contain, and makes all of it inspectable through a read-only API and
web screens. The API is GET-only; crawls and imports start from the command line. There is no
recommendation intelligence, publishing or learning yet. See
[PHASE_2_REPORT.md](PHASE_2_REPORT.md), [PHASE_1_REPORT.md](PHASE_1_REPORT.md),
[PHASE_0_REPORT.md](PHASE_0_REPORT.md) and
[NEXT_STEPS.md](NEXT_STEPS.md).

## Quick start (macOS)

```bash
colima start
make setup        # Python 3.12 venv + npm install + .env
make dev          # Postgres container on :5433 + migrations
make dev-api      # terminal 1 → http://127.0.0.1:8000/ready
make dev-web      # terminal 2 → http://127.0.0.1:3000/inventory
make ingest-file FILE=tests/fixtures/demo/research_site.jsonl   # load the synthetic demo site
make analyze      # Phase 2: classify, embed and relate (needs: ollama pull nomic-embed-text)
make verify       # the full quality gate
```

Full instructions: [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## Documentation

| Document | Purpose |
|---|---|
| [START_HERE.md](START_HERE.md) | Project brief and read order |
| [PRD.md](PRD.md) / [TRD.md](TRD.md) | Product and technical requirements — the governing specification |
| [CLAUDE.md](CLAUDE.md) / [AUTONOMY_AND_PERMISSIONS.md](AUTONOMY_AND_PERMISSIONS.md) | Constraints and the ALLOW / ASK / BLOCK permission model |
| [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) | Phases 0–9 |
| [docs/ARCHITECTURE_OVERVIEW.md](docs/ARCHITECTURE_OVERVIEW.md) | How the repository maps onto the TRD |
| [docs/CONTRACTS.md](docs/CONTRACTS.md) | The frozen shared contracts and their rules |
| [docs/adr/](docs/adr/) | Architecture decision records |
| [apps/desktop/README.md](apps/desktop/README.md) | Future Electron app — reserved, not installed (ADR-0008) |

## Non-negotiables

No paid APIs. Local inference via Ollama. PostgreSQL + pgvector as the system of record, through the
Apache-2.0 asyncpg driver. Every model
output crossing a boundary is validated with Pydantic. No LLM writes to production storage. Every
mutation is idempotent, version-aware, auditable and reversible. `AUTO_APPROVE` is disabled in v1.
The web UI is the canonical UI for a possible future Electron desktop app: it never imports Electron
and never touches the database (ADR-0008).
