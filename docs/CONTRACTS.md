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
| `intelligence.py` | Phase 2. Taxonomy policy schema (`TaxonomyPolicy`), `PageAnalysis`, embeddings/relationships/protections views, `LinkEligibility`, `IntelligenceReader` |
| `jobs.py` | Phase 2. `JobKind`, `JobRunSummary` / `JobRunDetail`, `JobCompletedPayload`, `JobReader` — the run record of every batch job from Phase 2 on |
| `inventory.py` | Phase 1. `ContentSourceProvider` + `CMSInventoryReader` protocols, `SourceDocument` / `SourceIssue`, `SiteScope`, `CrawlConfig`, `NormalizedPage`, the JSONL export format, inventory event payloads, API read models, `InventoryReader` (the API's only route to persistence) |

## Rules

**Base-model note (Phase 1):** `json_schema_serialization_defaults_required` on the contract bases marks every
always-present response field as required in OpenAPI. `PageInfo` also accepts and drops an incoming
`has_more` (a computed field), so a serialised `ListResponse` can be validated back by a typed client.
TypeScript clients therefore no longer treat `detail`, `error`, `title` and similar fields as
possibly absent. The wire format is unchanged.


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
| `job.completed` | operational, Phase 2 ([ADR-0010](adr/ADR-0010-content-intelligence.md)) — every finished generic job run (analysis, recommendation, verification, analytics import, evaluation, autonomy shadow), including failed ones |

`EventName` is a Pydantic enum, not a database enum, so additions need no migration.

### 13. Content inventory (Phase 1)

The design is in [ADR-0009](adr/ADR-0009-content-inventory.md). The rules that bind every
implementation:

- **Sources yield, never raise, per URL.** A `ContentSourceProvider` yields `SourceDocument` /
  `SourceIssue` in a deterministic order; an exception means the whole source failed. Sources are
  read-only: GET only, no forms, no authentication. `config_snapshot()` is persisted and must hold
  no secret.
- **Inventory never holds a mutation-capable CMS object.** It reads through `CMSInventoryReader`,
  never `CMSProvider`.
- **Normalisation is deterministic and LLM-free.** The same document gives a byte-identical
  `NormalizedPage`. `content_hash` covers content fields only, and `hash_version` is stored with it.
- **Versions are immutable.** `page_versions` and `existing_links` reject `UPDATE`/`DELETE` by
  trigger, and a version is written only when the hash changes.
- **The inventory API is GET-only** (a snapshot test asserts this). It reaches the database only
  through `InventoryReader`. `PageVersionDetail.body_html` must never be rendered as HTML by a
  client.

#### JSONL export format, version 1

UTF-8, one JSON object per line, blank lines ignored. Unknown keys are rejected.

```jsonl
{"format": "ken-inventory-export", "version": 1, "site_url": "https://example.com/", "allowed_hosts": ["www.example.com"], "exported_at": "2026-10-13T09:00:00Z", "generator": "cms-export 1.2"}
{"url": "https://example.com/reports/india-ev", "body_html": "<main>…</main>", "external_id": "cms-123", "page_type": "report", "title": "India EV Market", "publication_state": "published", "language": "en", "published_at": "2026-01-15T09:00:00Z", "metadata": {"author": "Research team"}}
```

- **Line 1 is an `ExportHeader`.** Its `site_url` host plus `allowed_hosts` define which links count
  as internal.
- **Each following line is an `ExportPageRecord`.** Only `url` and `body_html` are required. Every
  other field is an assertion that overrides what HTML parsing would infer.
- **A bad line becomes an `invalid_record` issue.** It never aborts the import, and the issue
  message never echoes field values.

#### Inventory events

| Event | `subject_type` / `subject_id` | Payload model |
|---|---|---|
| `page.created` | `page` / `str(page_id)` | `PageEventPayload` (version 1) |
| `page.updated` | `page` / `str(page_id)` | `PageEventPayload` (with `previous_version`, `previous_content_hash`) |
| `page.deleted` | `page` / `str(page_id)` | `PageDeletedPayload` — a 404/410 observation, on the transition only |
| `crawl.completed` | `ingestion_run` / `str(run_id)` | `CrawlCompletedPayload` — every run, of every source type, including failed runs |

`emitted_by` is `ken-ingest` and `correlation_id` is the run id. Payloads are always built from
these models (`model_dump(mode="json")`). No event name was added: `crawl.completed` covers file
and CMS imports via `source_type` (rule 12).

### 14. Content intelligence (Phase 2)

Design: [ADR-0010](adr/ADR-0010-content-intelligence.md). Modules: `intelligence.py`, `jobs.py`.

- **Deterministic analysis.** Classification, summaries and relationships are code over the
  versioned taxonomy policy (`policies/taxonomy/`). Every analysis records
  `analysis_version = "tax{N}-alg{M}"` and is immutable. LLM annotations (Phase 4 onward) are
  marked `extractor="llm"`.
- **The embedding dimension is the provider's.** The column is dimension-less, and every query
  filters on the provider-reported (model, dimensions). No code hard-codes a dimension.
- **Eligibility is computed at read time** (`LinkEligibility`) and never stored. Any active
  protection blocks a page as both source and target.
- **Relationships are derived and version-stamped**, and replaced per run for every edge touching
  an analysed page. Nothing may reference their ids.
- **Every batch job from Phase 2 on is a `job_runs` row**, emits `job.completed`, and holds a
  per-kind advisory lock.
- **The intelligence and jobs APIs are GET-only.** `/intelligence/search` returns 503
  `model_unavailable` when the embedding model is unreachable; it never falls back silently to
  another model.

### 15. Recommendations (Phase 3)

Design: [ADR-0011](adr/ADR-0011-recommendation-engine.md). Module: `recommendations.py`.

- **Deterministic engine.** Recommendations come from retrieval, filters, a transparent score and
  deterministic placement. No LLM is involved, and anchors are copied verbatim from the source;
  nothing is fabricated.
- **`LinkPlan` content is immutable,** enforced by a DB trigger on every non-status column, with
  DELETE rejected. Status moves only along `ALLOWED_TRANSITIONS`, and every move is an append-only
  `link_plan_transitions` row. An edit supersedes the plan; it never mutates it.
- **One open plan per pair and per anchor span** (partial unique indexes). Rejected pairs cool down
  for `rejection_cooldown_days`.
- **`plan.id` is the stable link identifier.** It becomes `LinkChange.change_id` (Phase 6) and the
  analytics link id (Phase 7).
- **Confidence is raw** (`calibration_version = None`) until Phase 8. No threshold may treat raw
  confidence as calibrated.
- **Actors** on transitions and events are `user:<id>`, `agent:<name>@<version>` or
  `system:<component>` (`ACTOR_PATTERN`).
- **The recommendations API is GET-only in Phase 3.** Approve, reject and edit arrive in Phase 5,
  behind authentication.

| Event | `subject_type` / `subject_id` | Payload |
|---|---|---|
| `link.proposed` | `link_plan` / `str(plan_id)` | `LinkPlanEvent` |
| `job.completed` | `job_run` / `str(run_id)` | `JobCompletedPayload` (kind `recommendation`) |
