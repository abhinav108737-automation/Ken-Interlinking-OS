# Tests — Ken Interlink OS

> **Always use the project virtualenv: `.venv/bin/pytest`, `.venv/bin/ruff`,
> `.venv/bin/mypy`.** The system `python3` on this machine is 3.9. The project
> requires Python 3.12 (`pyproject.toml`: `>=3.12,<3.13`), so never run tests
> with it.

## Tests vs evals

| | Tests (`tests/`) | Evals (`evals/`) |
|---|---|---|
| Question | Is the deterministic code **correct**? | Is the probabilistic decision **good**? |
| Result | Pass or fail; any failure blocks the build | Scores and rates against a baseline |
| Examples | Ranking math, eligibility, URL normalization, mutation generation, contract shapes | Destination choice, anchor choice, critic verdicts, no-action |
| Model | Always `MockModelProvider` | Replayed recorded fixtures by default; live is opt-in |

The TRD §22 "agent" tests (recorded fixtures for candidate selection, critic
and no-action) are still **tests**. They assert that a node is wired correctly
and that its typed boundaries hold for a given recorded response. Judging
whether that response is a *good* decision across a dataset is an eval. See
[`evals/README.md`](../evals/README.md).

## Layout

| Directory | Scope | I/O |
|---|---|---|
| `tests/unit/` | Pure logic: config, policy data, and later ranking, eligibility, placement and mutation generation (TRD §22 "Unit") | **None.** No database, network, model server or filesystem writes. |
| `tests/integration/` | **Phase 0 today:** migration up→down→up round-trip, ORM↔migration parity, events repository, DB-backed readiness probes, worker heartbeat. **Later (TRD §22):** crawl→DB, page→embedding, candidate→plan, approval→deployment, deployment→verification. (No endpoint is yet tested against a real DB; endpoint tests use fakes.) | Project PostgreSQL on **port 5433**, test database **`ken_interlink_test`**. Auto-skips when unreachable **unless `KEN_REQUIRE_INTEGRATION_DB=1`** — set by `make test-all`, `make test-integration`, `make verify` and CI, where unreachable is a **failure**. |
| `tests/contract/` | Enforces the frozen contracts (docs/CONTRACTS.md): `extra="forbid"`, async keyword-only protocols, mandatory `operation_id` / `expected_version`, the TRD §15 event vocabulary, and the committed OpenAPI snapshot `contracts/openapi/openapi.json` | None |
| `tests/fixtures/` | Shared, synthetic test data, such as `fixtures/cms/pages/` for the mock CMS | Read-only data |

Proposed (not yet created): `tests/agent/` for TRD §22 recorded-fixture agent
tests in Phase 4, and `tests/e2e/` for mock-CMS end-to-end tests in Phase 6.

### Database for integration tests

- This is the project container (`docker compose up -d postgres`), host port
  **5433**. **Never use 5432**: that is a separate, pre-existing PostgreSQL on
  this machine (docs/adr/ADR-0003.md).
- `ken_interlink_test` is created by
  `infrastructure/docker/postgres/init/01-create-test-database.sql`, so tests
  can migrate up and down without touching `ken_interlink`.
- CI points at it with `KEN_TEST_DATABASE_URL`
  (`.github/workflows/ci.yml`).
- Destructive database commands against any non-test database are ASK-level
  (CLAUDE.md).

## Markers

Declared in `pyproject.toml`. `--strict-markers` is on, so an undeclared marker
fails collection.

| Marker | Meaning |
|---|---|
| `integration` | Needs reachable PostgreSQL. A bare `pytest` run auto-skips when absent; with `KEN_REQUIRE_INTEGRATION_DB=1` (the gate) it fails instead, so a dead database can never produce a false green. |
| `live` | Needs a live local Ollama daemon. Runs only with `KEN_RUN_LIVE_OLLAMA=1`. |

Unmarked tests are unit or contract tests.

## Running

```bash
# Unit + contract only: no database, no network (= make test)
.venv/bin/pytest -m "not integration and not live"

# Integration only: needs Postgres on :5433 (= make test-integration)
.venv/bin/pytest -m integration

# Everything except live-model tests; this is what CI runs (= make test-all)
.venv/bin/pytest -m "not live"

# Live tests against host Ollama: opt-in, slow, local only (= make test-live)
KEN_RUN_LIVE_OLLAMA=1 .venv/bin/pytest -m live

# One file or test
.venv/bin/pytest tests/contract/test_frozen_contracts.py -q
```

Quality gates that go with them:

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy
```

## Rules

- **Tests and CI use `MockModelProvider` by default** (`KEN_MODEL_PROVIDER=mock`).
  It is deterministic and needs no model server. CI never runs `live` tests.
- **No paid APIs, ever.** No test may call OpenAI, Anthropic, Gemini or any
  paid or hosted service (CLAUDE.md constraints 1–3).
- **Every new endpoint requires tests** (CLAUDE.md constraint 14).
- `/health` must be tested with a broken database and still return 200
  (docs/CONTRACTS.md rule 7).
- **No secrets in fixtures or assertions.** Use obviously fake values.
  Payloads must never echo credentials (docs/CONTRACTS.md rule 9).
- **No real production or customer data** in `tests/fixtures/` without
  explicit user approval.
- **Never weaken a test or a validator to make the build pass.** That is
  BLOCK-level (AUTONOMY_AND_PERMISSIONS.md).
- `assert` is allowed in `tests/` only (ruff `S101` per-file ignore). Never use
  it for validation in production code.
