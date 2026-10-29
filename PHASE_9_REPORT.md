# Phase 9 Report â€” Autonomy-first publishing (and the Qdrant vector layer)

**Status:** verified on 2026-10-29. Design: [ADR-0018](docs/adr/ADR-0018-autonomy-first-publishing.md)
(autonomy) and [ADR-0017](docs/adr/ADR-0017-qdrant-vector-architecture.md) (Qdrant). Also relevant:
[ADR-0019](docs/adr/ADR-0019-critic-outcomes.md).

**Read this first.** Like Phase 8, this phase was implemented in an earlier session (commit `ff08b6c`).
This report records **verification** and a first **shadow-mode** run on real content. The autonomy code was
not re-reviewed line by line, and **nothing was published**: the CMS provider is the mock and the mode is
`shadow`.

## 1. What exists

| Area | Where | Size |
|---|---|---|
| Autonomy engine | `services/ken_services/autonomy/` â€” engine, risk, calibration, budget, blocks, facts, override, publish, job, CLI | 16 files, about 4,800 lines including the DB layer |
| Schema | migration `0010_qdrant_and_autonomy` (autonomy decisions, publish attempts, link-graph metrics; pgvector removed) | â€” |
| Vector layer | `services/ken_core/providers/vectors/qdrant.py` (491 lines); PostgreSQL stays the system of record, Qdrant is a derived index | â€” |
| Tests | `tests/unit/autonomy`, `tests/contract/test_autonomy_contracts.py`, `tests/integration/test_autonom*.py`, `test_agent_critic_autonomy.py`, `tests/unit/test_api_autonomy.py` | 336 passed |

## 2. The model, in one paragraph

ADR-0018 replaced the human-approval inbox as the normal path. Every plan gets an **autonomy decision**:
deterministic hard blocks first; the critic may only decline or tighten; risk may only rise; calibration may
only lower confidence; the mode's floors may only BLOCK. Modes are `shadow` (record what *would* happen),
`guarded` (publish only low-risk, high-confidence, calibrated decisions) and `autonomous`. People remain the
**control plane**: overrides, policy and skill promotion, and rollback. They are not a per-link approval step.

## 3. Safety defaults, checked in the live configuration

| Setting | Value here | Meaning |
|---|---|---|
| `KEN_AUTONOMY_MODE` | not set â†’ default `shadow` | publishes nothing |
| `KEN_CMS_PROVIDER` | `mock` | there is no production write path |
| `KEN_AUTO_APPROVE_ENABLED` | `false` | retired flag; its validator refuses `true` |
| `autonomous` mode | needs `KEN_AUTONOMY_AUTONOMOUS_ACKNOWLEDGED=true`; refused against an `http` CMS with writes unless `KEN_AUTONOMY_ALLOW_PRODUCTION=true` | cannot be selected implicitly |

## 4. Verification

- 336 Phase 9 tests pass (unit, contract, integration against real Postgres). The full suite was also green
  (see [PHASE_8_REPORT.md](PHASE_8_REPORT.md) Â§3, 6,187 passed plus the three fixes).
- ruff and mypy clean.

## 5. First shadow run on real content (an observation, not a gate)

Database `ken_real500`, 126 plans, `KEN_AUTONOMY_MODE` unset (shadow).

| Result | Count |
|---|---|
| Plans considered | 126 (1 declined, 125 deferred), **0 published**, 0 errors |
| Decision recorded (shadow always blocks) | 125 `block` |
| `would_have_decision` | 95 `block` (75 critic-accepted, 20 critic-rejected) and **30 `auto_publish_with_extra_verification`** |
| Risk tier | all `medium` |

**Why so few would publish.** No plan has a calibration version (none exists: calibration is fitted from
evaluation data, which needs real human judgments). ADR-0018 deliberately keeps "uncalibrated" as a risk signal
and a floor in `guarded` mode. So on a fresh deployment `guarded` would publish nothing, and `autonomous` would
publish the 30 critic-accepted plans with extra verification. Calibration is therefore on the critical path to
any publishing, which is the intended behaviour.

## 6. Vector layer â€” observation

Qdrant is healthy (`ken_pages`: 67 points, the demo corpus; `ken_cluster50_8b`: 50). The real corpus is
**not indexed** in Qdrant; the analysis and recommendation runs on real pages used PostgreSQL embeddings
(at least 78 of the 126 plans include the semantic channel, so retrieval worked). Indexing real pages is a
separate step (`ken_services.vectors.cli index`), planned for the full inventory in its own collection.

## 7. Known gaps

- No calibration data â†’ `guarded` publishes nothing; nothing has measured the real publish path (the CMS is a
  mock; parity with Ken's real templates is NEXT_STEPS item 14).
- Real-CMS contract (item 12) and write credentials are still needed before any real publish.
- `review_performance` (ADR-0012 Â§42) is still deferred (item 23).
- Promotion of any policy or skill stays human and is currently refused by demonstration mode (item 20).
- Windows: 37 tests skip for platform reasons (POSIX modes, signal handlers, `make`); integration tests need
  `KEN_TEST_DATABASE_URL` with `127.0.0.1`.
