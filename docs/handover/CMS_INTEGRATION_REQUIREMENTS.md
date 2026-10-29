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

## 0. What to prepare, in priority order

Items 1 and 2 unblock us immediately and need no write access, so please start there. Nothing
below requires the interlinking system to be finished first — the two sides can be built in
parallel and joined at step 3.

| # | What we need | Why it is first | Blocks |
|---|---|---|---|
| 1 | **A JSONL export of ~50 real pages** (section 3) | Lets us run ingest → analysis → recommendations on real Ken Research content instead of demo data. No API needed: it is a file. | Everything downstream. Most valuable single item. |
| 2 | **Staging base URL + a READ-ONLY bearer token**, and `GET /internal/pages/{id}` working | Lets us read live page bodies and confirm our adapter talks to Django correctly. Read-only, so nothing can be changed. | Validation and dry runs |
| 3 | **`validate` and `link-changes` endpoints on staging** (sections 1 and 2) | Lets us dry-run a real link insertion and see the exact diff. Still no live writes: `dry_run: true` must change nothing. | The first end-to-end test |
| 4 | **`rollback` endpoint** | Required before any real write. We do not publish anything we cannot undo. | Enabling writes at all |
| 5 | **A write token for staging** | Only after 1–4 pass the section 6 checklist, and only on staging. | The first real published link |
| 6 | Production access | A separate decision by the project owner, after staging is proven. | — |

### Safety posture they should know

- **Writes are disabled by default on our side** (`KEN_CMS_WRITES_ENABLED=false`) and stay off
  until the owner turns them on. A read-only token is genuinely all we need to begin.
- **The first phase is manual approval.** Every link is reviewed by a person before it is
  published, even though the engine can run unattended. Unattended publishing is a later,
  separate decision.
- **Nothing is published that cannot be undone.** If a rollback is not possible for a given
  change, the system refuses to publish it at all. That is why item 4 is not optional.
- Every change we send carries an idempotency key, the page version we built it against, and the
  account that authorised it, so your audit log can always answer "who and when".

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

1. A JSONL export of about 50 pages, or the management command that produces one.
2. Staging base URL and a **read-only** token first; a write token only when the owner approves.
3. Confirmation of how `version` is produced (revision id, content hash, `updated_at`?).
4. Page ids vs URLs: are ids stable, and what is the id format?
5. How the CMS reports published, draft and protected, and which URL is canonical.
6. Rate limits and a request-size limit, if any.
7. Whether an edit goes live immediately or needs a separate publish step in Django.
8. Whether the page body is stored as HTML, and whether anything rewrites it on save
   (sanitisers, editors, template rendering). **This one matters:** we insert one `<a>` and
   leave every other byte identical, so a saver that reformats the HTML would break
   verification and rollback.

## 6a. CONFIRMED 2026-10-29: the CMS has a draft/publish workflow

The owner has confirmed two facts that change the integration. Both need answers before any
write is enabled.

**Fact 1 — an edit does not go live immediately.** Saving an edit produces a draft; a separate
publish step makes it live.

This breaks an assumption in our Phase 6 pipeline, which applies a change and then verifies it.
With a draft/publish CMS there are two different things to verify, and the gap between them is
a human action with no fixed duration:

- the **draft body** we just wrote (verifiable immediately), and
- the **live page** (only verifiable after someone publishes, which may be minutes or days).

What we need to know:

1. Does `POST /internal/pages/{id}/link-changes` write a **draft**, or publish directly?
2. Is there a **publish endpoint** we can call, or must a person publish inside Django?
3. Can `GET /internal/pages/{id}` return **both** the draft body and the live body, and say
   which is which? We need to tell them apart.
4. Does `version` change on save, on publish, or both?
5. On **rollback of an unpublished draft** — does reverting the draft leave the live page
   untouched, as we would expect?
6. Is there a webhook or a flag we can poll to learn that a page was published?

Until these are answered, our post-publish verification would either check a draft and report a
link as live when it is not, or re-fetch the live URL and report a false failure. Neither is
acceptable, so **verification timing is an open design item**, not a configuration setting.

**Fact 2 — the owner expects the page to be rewritten with the proposed content on approval.**
This needs clarification before build (see the note in the handover thread): inserting a link
leaves every other byte identical, whereas rewriting page content is a different operation with
its own ADR (ADR-0020) and its own approval rule. The endpoints in section 1 cover link
insertion only.

## 7. Questions we expect them to ask

- *"Do you need write access on day one?"* No. Items 1 and 2 of section 0 are read-only.
- *"Will this change page text?"* Not in this integration. It inserts links only. A separate
  proposal (ADR-0020) would insert the single word "market" into an anchor, and it is not part
  of this work.
- *"What happens if someone edits the page after you planned a link?"* We send the version we
  planned against; a mismatch must return 409 and change nothing. We then re-plan.
- *"How many requests?"* Low. One read per page analysed, then one validate and one apply per
  approved link. There is no polling loop.

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
