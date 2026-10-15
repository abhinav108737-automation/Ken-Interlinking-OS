# Ken Research Agentic Interlinking OS

A fully self-hosted, local-first system that will manage contextual internal body links across Ken Research
pages: observe → understand → recommend → approve → publish → verify → measure → learn.

**Current status: Phase 6 (controlled deployment) is complete**, built on Phases 1–5.

Phase 6 publishes approved links safely, through a CMS adapter:
- validate → dry-run preview → version-locked apply → post-publish verification → rollback;
- every step is idempotent, crash-safe (durable intent plus `make deployments-reconcile`),
  audited with typed events, and reversible, either per link or per deployment;
- HTML is changed by a deterministic **splice**: one `<a>` inserted, every other byte
  identical.

It runs end to end against a local **simulated CMS** and site. The production HTTP adapter is
contract-tested and disabled until you configure it and approve its use.

Phase 5 puts a human in charge:
- local, self-hosted sign-in with roles (viewer, reviewer, operator-admin) and no default
  accounts;
- a dashboard and a review inbox;
- plan detail with a before/after preview, where a reviewer can approve, reject, edit the
  anchor or replace the destination, each with structured feedback, or bulk-review;
- an audit log and read-only policy settings.

Every action is idempotent, version-aware, re-validated under lock and recorded as editorial
feedback. AUTO_APPROVE stays disabled. **Nothing is published yet:** that is Phase 6.

Phase 4 adds a supervised LangGraph workflow that **reviews** the engine's proposals. For each
page with unreviewed proposals it:
- triages the page deterministically;
- lets an optional local model (FAST role) choose which plans deserve a reviewer's time;
- critiques each plan with an optional local model (REASONING role) under a strict, validated
  output schema, falling back to a deterministic rule whenever the model is off, slow or wrong;
- re-checks every plan's invariants against live state;
- hands accepted and uncertain plans to the human queue (`awaiting_approval`) in one audited
  transaction.

Agents never create, approve, reject or publish a link. Every run, step, tool call, model call
and review is recorded and inspectable at `/agents`.

Phase 3 proposes contextual internal links deterministically. For each eligible page it:
- retrieves candidates through semantic, lexical, taxonomy and link-graph channels;
- applies safety and eligibility filters;
- scores them transparently from versioned policy data;
- places each link on a natural phrase **copied verbatim** from the page, never fabricated or
  misleading.

Each proposal is an immutable `LinkPlan`, with its rationale, score breakdown and history,
inspectable at `/recommendations`. **Nothing is published:** review is Phase 5 and publishing is
Phase 6.

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
[PHASE_6_REPORT.md](PHASE_6_REPORT.md), [PHASE_5_REPORT.md](PHASE_5_REPORT.md), [PHASE_4_REPORT.md](PHASE_4_REPORT.md), [PHASE_3_REPORT.md](PHASE_3_REPORT.md), [PHASE_2_REPORT.md](PHASE_2_REPORT.md), [PHASE_1_REPORT.md](PHASE_1_REPORT.md),
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
make recommend    # Phase 3: propose contextual links (nothing is published)
make agents       # Phase 4: agents review proposals → human queue (local model optional)
make user-create USERNAME=you ROLE=operator_admin   # Phase 5: no account exists until you create one
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
