# Django CMS integration: what the backend team needs to provide

- **Status:** DRAFT for the backend team, 2026-10-29.
- **Source of truth:** the project's own contract, not a new design. Every item below comes from
  `contracts/python/ken_contracts/cms.py`, `services/ken_core/providers/cms/http.py`
  (the production adapter, written and contract-tested but **disabled**) and `docs/CONTRACTS.md`.
- **Reference implementation of the CMS side:** `services/ken_core/providers/cms/simulated.py`,
  `html_engine.py` and `services/ken_services/deployment/cms_sim.py`. These are what a CMS has to
  do; port the behaviour to Django, do not reinvent it.
- **Start with a STAGING CMS.** Connecting to the production CMS needs the project owner's
  explicit approval first.

## 1. Four REST endpoints (required)

All under a base URL we are given. JSON bodies. Bearer token auth.

| Method and path | Request | Success response | Errors |
|---|---|---|---|
| `GET /internal/pages/{id}` | none | `CMSPage` | 404 unknown page |
| `POST /internal/pages/{id}/link-changes/validate` | `LinkChangeRequest` | `ValidationResult` | 400/422 with `issues` |
| `POST /internal/pages/{id}/link-changes` | `LinkChangeRequest` | `DeploymentResult` | **409** version conflict (include `expected` and `current`), 400/422 with `issues` |
| `POST /internal/deployments/{id}/rollback` | `operation_id`, `actor` | `RollbackResult` | **409** if the page changed since the deployment, 404 unknown deployment |

`{id}` is URL-escaped. Redirects are never followed by our client, so do not redirect.

### `CMSPage` (response of GET)
`page_id` (string), `url`, `title`, `body_html`, `version` (opaque string token), optional
`canonical_url`, `protected` (bool), `published_at`, `updated_at`, `metadata` (object).

- **`version` is opaque.** We never parse or compare it, only send it back. Any value that changes
  whenever the body changes works (a revision id or a content hash).
- **`protected: true`** means no mutation may target the page, whatever anyone approved.

### `LinkChangeRequest` (body of validate and apply)
`operation_id` (UUID, the **idempotency key**), `source_page_id`, `expected_version`,
`changes` (1 or more), `dry_run` (bool), `actor` (who authorised it, for your audit log).

Each change: `change_id`, `operation` (`insert` | `remove` | `replace`), `target_page_id`, `href`,
`anchor_text`, `locator` (`block_selector`, `occurrence_index`, `context_hash`), and for
remove/replace `previous_href` and `previous_anchor_text`.

## 2. Behaviour the CMS must guarantee

1. **Idempotent.** Replaying the same `operation_id` returns the original result with
   `idempotent_replay: true` and changes nothing.
2. **Version-locked.** If `expected_version` differs from the page's current `version`, return 409
   and change nothing.
3. **Dry run.** `dry_run: true` must change nothing and report status `no_op`.
4. **Surgical edit.** Insert only the `<a href>` around the anchor text; **every other byte of the
   body stays identical**. Locate the text by `context_hash` (hash of the block's text) plus
   `occurrence_index`. If the block is not found, is ambiguous, the anchor is not at that
   occurrence, or it sits inside an existing link, a table, hidden content or an excluded element,
   reject with an `issue` code. The simulated CMS shows the exact rules.
5. **Reversible.** The result carries `deployment_id`, `previous_version`, `new_version`. Rollback
   restores the previous body. If the page has changed since, return 409 and do **not** overwrite.
6. **Audit.** Record `actor`, `operation_id`, time and the diff.
7. **Never mutate a `protected` page.**
8. **Same page body for read and write.** `GET` must return the body the edit will apply to.

## 3. Getting pages into the system (also required)

The adapter has **no "list all pages" call**. Pages are brought in by either:

- **A JSONL export (preferred to start).** One header line, then one line per page. Only `url` and
  `body_html` are required; `external_id`, `page_type`, `title`, `publication_state`, `language`,
  `published_at`, `metadata` are optional. A Django management command can produce it. Format in
  `docs/CONTRACTS.md` ("JSONL export format, version 1"). Unknown keys are rejected.
- **or a crawl** of the public site (one configured site only).

Please also tell us: how to tell **published vs draft**, which pages are **protected**, and which
URL is canonical.

## 4. Configuration we will set on our side (no secrets in git)

| Setting | Value |
|---|---|
| `KEN_CMS_PROVIDER` | `http` (currently `mock`) |
| `KEN_CMS_BASE_URL` | staging Django URL |
| `KEN_CMS_TOKEN` | service token (stored in `.env`, never committed) |
| `KEN_CMS_ALLOW_REMOTE` | `true` (needed for a non-loopback host) |
| `KEN_CMS_WRITES_ENABLED` | stays `false` until the owner approves writes |
| `KEN_CMS_TIMEOUT_S` | 10 |

## 5. What the backend team should send back

1. Staging base URL and a **read-only** token first; a write token only when the owner approves.
2. Confirmation of how `version` is produced.
3. A JSONL export of a sample (about 50 pages) or the command to make one.
4. Page ids vs URLs: are ids stable, and what is the id format?
5. How the CMS reports published, draft and protected.
6. Rate limits and a request-size limit, if any.
7. Whether an edit goes live immediately or needs a publish step.

## 6. Acceptance checklist (run against staging)

- [ ] GET returns a valid `CMSPage` for 3 known pages, including a protected one.
- [ ] Validate returns `valid: true` for a good change and an issue code for each bad case.
- [ ] Apply with a stale `expected_version` returns 409 and changes nothing.
- [ ] Apply the same `operation_id` twice: the second is an idempotent replay.
- [ ] After apply, only one `<a>` was added; a byte-for-byte diff shows nothing else changed.
- [ ] Rollback restores the original body exactly.
- [ ] Rollback after a later manual edit returns 409 and overwrites nothing.
- [ ] A protected page rejects every change.
- [ ] The bearer token is required; a wrong token is rejected.
