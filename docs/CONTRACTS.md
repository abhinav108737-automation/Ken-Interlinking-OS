# Shared Contracts

The modules under `contracts/python/ken_contracts/` are **frozen**. Every other package imports from
them; they import from nothing else in the repository.

A specialist that needs a contract changed **proposes** the change to the lead orchestrator. It does
not edit the file. See MULTI_AGENT_ORCHESTRATION.md, "Shared Contracts".

`tests/contract/test_frozen_contracts.py` enforces the rules below. A freeze without tests is a
convention, not a freeze.

## Module map

| Module | Contract |
|---|---|
| `common.py` | `ContractModel` / `FrozenModel` bases, identifier aliases, `AsyncClosable`, `HealthStatus`, `CheckKind`, `ServiceIdentity` |
| `health.py` | `HealthResponse`, `ReadinessResponse`, `DependencyCheck`, `MetaResponse`, `ReadinessStatus` |
| `model.py` | `ModelProvider` protocol, `ModelRole`, `Message`, `GenerationOptions`, `Completion`, `StructuredResult[T]`, `EmbeddingResult`, `ProviderCapabilities`, `ProviderHealth`, model errors |
| `cms.py` | `CMSProvider` protocol, `CMSPage`, `LinkChange`, `LinkChangeRequest`, `ValidationResult`, `DeploymentResult`, `RollbackResult`, CMS errors |
| `analytics.py` | `AnalyticsProvider` protocol (read-only, **no implementation until Phase 7**) |
| `events.py` | `EventName` (TRD §15 vocabulary + governed additions), `EventEnvelope`, `PromotionAuditPayload` |
| `errors.py` | `ErrorResponse`, `ErrorCode`, `ListResponse[T]`, `PageInfo` |

## Rules

### 1. Every model forbids unknown keys

Every contract model inherits `ContractModel` or `FrozenModel`, both of which set `extra="forbid"`.
An unexpected key fails at the boundary rather than being silently dropped (TRD §6).

### 2. Provider protocols are async, keyword-only, and closable

- **Async-only.** FastAPI, httpx and LangGraph are async; a sync protocol forces `run_in_executor`
  everywhere by Phase 4.
- **Keyword-only arguments.** A new parameter can never shift an existing position.
- **Pydantic returns.** A new optional response field is never a breaking change.
- **`aclose()`** via `AsyncClosable`, so a caller holding only the protocol can release resources.
- **Typed exceptions**, never sentinel return values.

### 3. Models are selected by role, never by name

`ModelProvider` methods take `role: ModelRole` (`FAST` / `REASONING` / `EMBEDDING`). Resolution to a
concrete model happens inside the provider, from `KEN_MODEL_FAST` / `KEN_MODEL_REASONING` /
`KEN_MODEL_EMBEDDING`. Business logic never names a model (CLAUDE.md, Model Design Rule).

### 4. LLM output crosses boundaries only as validated models

`complete_structured(schema=...)` is the default call shape. `Completion.text` is raw and must be
validated by the caller before it influences any decision. No raw model output may trigger a
database mutation, publication, rollback, policy change or permission change (TRD §6).

### 5. Mutations are idempotent, version-aware, auditable and reversible by construction

| Property | Where it is enforced |
|---|---|
| Idempotent | `LinkChangeRequest.operation_id` is mandatory |
| Version-aware | `LinkChangeRequest.expected_version` is mandatory; mismatch raises `CMSVersionConflictError` |
| Validate first | `dry_run` and `validate_link_changes` exist from day zero |
| Auditable | `actor` on requests and rollbacks; `EventEnvelope` carries `event_id`, `emitted_by`, `correlation_id` |
| Reversible | `DeploymentResult.previous_version` is mandatory; `rollback()` is part of the protocol |

### 6. External identifiers are opaque

`PageId`, `DeploymentId` and `LinkId` are strings owned by an external system: round-trip them
exactly, never parse, compare or generate them. `CMSPage.version` is likewise opaque, so a real CMS
can move from an integer to an ETag without a contract change.

`OperationId` and `CorrelationId` are UUIDs because the system mints them itself.

### 7. `/health` performs zero I/O

`GET /health` is liveness. It must never touch the database, the model server or the CMS. If a
database query is added there, a transient PostgreSQL blip becomes a container restart loop. A test
calls `/health` with a broken database and asserts 200.

### 8. `/ready` has one body shape

`GET /ready` returns the same `ReadinessResponse` shape on success and failure, so clients never
branch on status code to parse.

| Aggregate | HTTP |
|---|---|
| `ready` | 200 |
| `degraded` (all required checks ok, some optional not) | 200 |
| `not_ready` (any required check failed) | 503 + `Retry-After: 5` |
| A probe raised | 503, that check `status: "error"` — never 500 |

### 9. Payloads never carry secrets

`DependencyCheck.detail`, `MetaResponse.settings` and `ErrorResponse` must never contain credentials.
`Settings.public_settings()` is an explicit allowlist that parses the database URL with
`urllib.parse.urlsplit` and publishes only host, port and database name. String splitting is unsafe
here: a libpq-style DSN can carry `?password=` in its query string. A regression test covers this.

### 10. Errors and collections have one shape each

Every error body is an `ErrorResponse` with a stable `ErrorCode`; clients branch on `code`, never on
`message`. Every collection body is a `ListResponse[T]` with offset/limit `PageInfo`. Neither may
carry a stack trace, exception class name or database message.

### 11. Promotion is governed at the contract boundary

`skill.promoted` and `policy.promoted` are the audit events for ASK-level actions (PROMOTE_SKILL,
PROMOTE_POLICY in AUTONOMY_AND_PERMISSIONS.md). Their lifecycles:

```
skill.proposed  -> offline/human evaluation -> skill.promoted
policy.proposed -> offline/human evaluation -> policy.promoted
```

`EventEnvelope` **rejects** a promotion event unless its payload is a complete
`PromotionAuditPayload`: `artifact_kind` matching the event, `name`, `version`, optional
`previous_version` (what a rollback restores), a non-empty **human** `promoted_by`, and a non-empty
`evaluation_ref` citing the passing evaluation (PRD §9: activation must pass evaluation). Its
`subject_type`/`subject_id` must identify the promoted artifact, so its whole history is one indexed
query. Proposals carry no such requirement — agents may propose (ALLOW), never promote.

Enforcement points, because construction-time validation alone was bypassable:

- **construction** — the `EventEnvelope` validator;
- **field assignment** — `EventEnvelope` is frozen, so assignment is refused and leaves the object unchanged;
- **persistence** — `append_event` re-validates every envelope before inserting. This catches
  `model_copy(update=…)` (which skips validation) and in-place payload mutation. It matters doubly in an
  append-only log: one invalid row would otherwise make `list_events` fail for every reader, forever.

Text fields are whitespace-stripped before their `min_length` check, so `"   "` is not an authoriser, and
the **normalised** payload is what gets stored — so the stored `name` is exactly the one checked against
`subject_id`.
Unknown payload keys are rejected: adding context to a promotion record (e.g. evaluation metrics) is a
deliberate contract change.

No skill or policy learning behaviour exists yet (Phase 8); this is the contract it must satisfy.

### 12. Adding an event name is a governed decision

`EventName` starts from the TRD §15 vocabulary. A name outside it is added only by an explicit,
recorded decision — never by a specialist on its own. Current additions:

| Event | Decision |
|---|---|
| `worker.heartbeat` | operational; ADR-0005 |
| `skill.promoted` | user decision, post-Phase-0 cleanup (2026-10-13) — closes the TRD gap where PROMOTE_SKILL had no audit event |

`EventName` is a Pydantic enum, not a database enum, so additions need no migration.
