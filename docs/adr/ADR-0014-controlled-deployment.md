# ADR-0014 — Phase 6 controlled deployment: deterministic HTML mutation through the CMS adapter

- **Status:** Accepted (Phase 6); amended before fan-out by the pre-fan-out architecture review (§8, binding; it supersedes conflicting text in §§1–6)
- **Date:** 2026-10-14
- **Deciders:** orchestrator (under the user's 2026-10-14 autonomous-delivery authorisation)
- **Related:** PRD §§7.10, 7.11; TRD §§11, 13, 15, 23; ADR-0011 (plans, locators), ADR-0013
  (review), CONTRACTS `cms.py`

## Context

Approved plans must reach the page safely: validated, permission-checked, dry-run,
version-locked, applied exactly once, verified, audited and reversible. TRD §11 forbids an LLM
anywhere near HTML mutation, publishing or rollback. The real Ken CMS is **not** available.
Connecting to it, using its credentials or mutating it are ASK actions. Phase 6 therefore
builds three things:
- the complete flow;
- a local simulation that behaves like a CMS;
- a production adapter contract and configuration, which stays inert until the user enables
  it.

## Decision

### 1. The deployment flow (one source page per deployment)

```
VALIDATE → permission → dry-run → expected-version → apply → verify → event/audit → (rollback)
```

1. **Request.** An operator selects 1–20 `approved` plans of ONE source page. A client
   `operation_id` makes the request idempotent, exactly as in ADR-0013 (a fingerprint, replay,
   and a 409 on reuse).
2. **Permission.** The new `Permission.DEPLOY` belongs to `operator_admin` only. Reviewers
   approve; operators deploy. The actor comes from the session.
3. **Validate.** Under the per-source advisory lock and each plan's row lock, the deterministic
   checks are:
   - the plan is `approved`;
   - it is fresh or rebasable against the inventory (`check_plan_freshness`);
   - durable validity holds (`validity.py`: protections, eligibility, already linked);
   - the CMS page exists, is not protected by the CMS and passes the target and href rules
     (§3);
   - the mutation dry-runs cleanly on the CMS page's CURRENT HTML (§2).

   Any failure gives a typed issue. Nothing is written to the CMS, and the plan stays
   `approved`, or becomes `expired` when it is stale.
4. **Dry run.** `POST /deployments/dry-run` runs steps 1–3 and calls
   `CMSProvider.validate_link_changes`. It returns a preview: block text before and after
   with the anchor marked, an HTML snippet before and after (rendered as TEXT in the UI), and
   any issues. It writes nothing.
5. **Expected version.** The CMS page `version` (opaque) is read in step 3 and passed as
   `expected_version`. A mismatch at apply time (`CMSVersionConflictError`, TRD §13's 409)
   moves every plan in the request to `expired` ("cms version conflict"); the engine
   regenerates from current content.
6. **Apply.**
   - Plans move `approved → validated` (`system:deployment-service`). Then
     `CMSProvider.apply_link_changes` is called with `change_id = plan.id` (the stable link id,
     ADR-0011) and the deployment's operation id.
   - On success, plans move `validated → deployed`, a `deployments` row records the previous
     and new CMS versions, and `link.deployed` is emitted per plan.
   - On a CMS validation error, plans move `validated → failed` and `link.failed` is emitted.
7. **Verify.** This runs right after apply, and again on demand (`POST /deployments/{id}/verify`).
   - It reads the page back: `CMSProvider.get_page`, plus an HTTP GET of the published URL when
     a verification base URL is configured.
   - Each link must appear EXACTLY once: `href` resolves to the target page, the anchor text is
     verbatim, it sits in the expected block (`context_hash` of the block text without the
     link equals the plan's), it is not hidden (no `hidden`, `aria-hidden` or inline
     `display:none` / `visibility:hidden` ancestor), and it is not nested in another link.
   - The target URL must answer 200 when HTTP verification is configured.
   - All checks pass → `deployed → verified` and `link.verified`. Any check fails →
     `deployed → failed` and `link.failed`, and the UI offers rollback.
8. **Rollback.** `POST /deployments/{id}/rollback` (`deploy`) needs a structured reason. It
   calls `CMSProvider.rollback(operation_id = the deployment's)` and moves plans
   `deployed | verified | failed → rolled_back` (`system:deployment-service`, with the user in
   the note). It emits `link.rolled_back`. Rollback is idempotent: a second rollback is
   `no_op` and changes nothing.

There is no automatic deploy. Nothing deploys without an operator request: AUTO_APPROVE stays
disabled, and no job or agent calls the deployment service.

### 2. Deterministic HTML mutation (`ken_services.deployment.mutation`)

The CMS stores FULL page HTML. Our inventory stores the normalised content root. The mutation
service therefore works on the CMS page's own HTML and never trusts the inventory copy.

- **Locate.** Run the inventory's block walk (`extract_blocks` semantics) over the CMS HTML.
  Choose the block whose text hash equals the plan's `context_hash`. If several blocks match,
  prefer the `block_id` hint; if it is still ambiguous, refuse with `ambiguous_block`. Then
  find the anchor by `phrase_occurrences(anchor_text, block.text)[occurrence_index]`. It must
  equal the plan's `[start, end)` and pass `span_is_safe`, so it lies inside one run, with no
  excluded ancestor and no existing `<a>`.
- **Map to source.** Map the block-text span back to EXACT character offsets in the original
  HTML string, using the stdlib `html.parser.HTMLParser` with source positions. The span must
  lie inside ONE text node, contain no character or entity reference, and NFC/whitespace
  collapsing must map one to one over the span. Otherwise refuse: `anchor_not_in_single_text_node`
  or `unmappable_span`.
- **Splice, never re-serialise.** The output is the input string with `<a href="…">` inserted
  before the span and `</a>` after it. Every other byte is identical. The `href` is
  HTML-attribute-escaped. The element carries `data-ken-link-id="<plan id>"`, so verification
  and rollback can find it again; `data-` attributes are inert.
- **Remove and replace** (for rollback and for a CMS that only stores HTML) are the inverse
  splices. They are located by `data-ken-link-id` and checked against `previous_href` and
  `previous_anchor_text`.
- **Idempotence.** Applying a change whose `data-ken-link-id` is already present with the same
  href and anchor is a no-op (`already_applied`). The same id with different content is a
  refusal (`conflicting_link`).
- **Self-check.** After splicing, re-running the block walk must give the SAME block text, and
  the span must now be a run with ancestor `a`. Otherwise refuse with `self_check_failed`. The
  function is pure and total: it returns a result or typed issues, and never raises on
  malformed HTML.

### 3. Target and href rules

- `href` is built from the target page's canonical URL. It is http(s), on the source page's
  host or the configured allowed hosts, and has no credentials and no `javascript:`/`data:`.
- The anchor is non-empty and passes the anchor rules.
- The target is eligible as a target in the inventory and not protected. With HTTP
  verification configured, it must answer 200 at deploy time (`broken_target`).

### 4. CMS providers

| Provider | Use | Writes |
|---|---|---|
| `MockCMSProvider` (in memory) | unit tests | real HTML mutation through §2 (upgraded from the Phase 0 protocol-only mock) |
| `SimulatedCMSProvider` (PostgreSQL schema `cms_sim`) | **local end-to-end simulation**: seeded from the inventory (`make cms-sim-seed`), and served as a local website by `make cms-sim-serve` on a free loopback port, so the Phase 1 crawler can re-read it and HTTP verification can fetch it | real HTML mutation, persisted, with an operation ledger and rollback snapshots |
| `HttpCMSProvider` | **production adapter** for TRD §13's endpoints (`GET /internal/pages/{id}`, `POST …/link-changes/validate`, `POST …/link-changes`, `POST /internal/deployments/{id}/rollback`), with Bearer credentials from `KEN_CMS_TOKEN` | **disabled unless `KEN_CMS_WRITES_ENABLED=true`**, which also requires `KEN_CMS_BASE_URL` and the token. Tested only against a local fixture server. Pointing it at the real Ken CMS is an ASK action, and is not done. |

The CMS is chosen by `KEN_CMS_PROVIDER` (`mock` | `simulated` | `http`). The default stays
`mock`.

### 5. Data, integrity and least privilege (closing Phase 3/5 deferrals)

- **Migration 0007:**
  - `deployments`: id, operation id (UNIQUE) plus request fingerprint, source page, CMS page
    id, status, the actor and user, previous and new CMS versions, the dry-run snapshot,
    timestamps, error code. Only status, verification and timestamps change after insert, and
    finished rows are frozen.
  - `deployment_items`, append-only: plan id and change id, and the outcome per plan.
  - `deployment_verifications`, append-only: each verification run and its checks.
  - the `cms_sim` schema;
  - **link-plan status integrity in the database**: a DEFERRABLE INITIALLY DEFERRED
    constraint trigger on `link_plans` rejects any status change that is not in
    `ALLOWED_TRANSITIONS`, or that has no matching `link_plan_transitions` row in the same
    transaction. A direct `UPDATE link_plans SET status` is therefore impossible, which closes
    the Phase 3 QA M2 gate.
- **Read-only GET path.** Readers run their transactions `READ ONLY` (a separate session
  factory), so a bug in a GET handler cannot write.
- **Least-privilege database role:** documented as a production deployment requirement (an
  app role with DML on application tables only, no DDL; migrations run as the owner). Creating
  roles on a shared or production server is an operator action, and is not done here.
- **Job reaper.** `make jobs-reap` fails `running` `job_runs` whose kind's advisory lock is not
  held, closing the Phase 1/2 "stays running" gap.

### 6. Verification transport: HTTP/HTML now; browser verification deferred

Verification parses the served HTML with the same block walk. Inline styles and the hidden
attributes cover the documented `display:none` gap. A headless browser (Playwright) is **not**
adopted: it would add a large browser download and process for checks that HTML parsing
answers deterministically. Revisit this only if Ken's pages render links client-side.

### 7. API and UI

| Permission | Routes |
|---|---|
| `read` | `GET /deployments`, `GET /deployments/{id}`, `GET /recommendations/{id}/deployments` |
| `deploy` | `POST /deployments/dry-run`, `POST /deployments`, `POST /deployments/{id}/verify`, `POST /deployments/{id}/rollback` |

UI:
- deployment history and detail (items, previous and new versions, the verification checks,
  the dry-run preview as text, and rollback with a reason);
- a "Deploy" flow on approved plans (dry-run preview → confirm), visible to `deploy` only;
- the plan's deployment state shown on plan detail.

### 8. Amendments from the pre-fan-out architecture review (binding)

**Durable intent and reconciliation.** A deployment uses two transactions around the CMS
call, under the source deployment lock below.
1. **Transaction 1:**
   - validate;
   - move the plans `approved → validated`;
   - insert the `deployments` row as `applying`, storing the EXACT serialised
     `LinkChangeRequest` and its fingerprint;
   - commit.
2. **The CMS call.**
3. **Transaction 2:** record the outcome, and emit its events in the same transaction:
   - applied → `applied`, plans `deployed`;
   - a validation error → `failed`, plans `failed`;
   - a version conflict → `conflict`, plans `expired`.

**A timeout or unknown outcome leaves the row `applying`.** It is never `failed`.

**Reconciliation.**
- `make deployments-reconcile` resends the STORED request byte for byte for `applying` (and
  `rolling_back`) rows older than T:
  - a replay result → `applied`;
  - a conflict → `conflict` / `expired`;
  - a validation error → `failed`.
- **Provider contract** (contract-tested for every provider): replaying an applied
  `operation_id` with the identical request returns the stored result, even if the page has
  moved since.

**Per-source deployment lock.** A session-level `pg_advisory_lock("ken.deploy.source:<id>")`
on a dedicated connection is held across transaction 1, the CMS call and transaction 2. A
concurrent request gets a retryable 409 `deployment_in_progress`, and its plans are left
unchanged, never expired. Inside each transaction, the ADR-0013 per-source plan-writer xact
lock is also taken.

**`VALIDATED` belongs to the deployment service.** The pipeline's invalidity and stale expiry
and the review service SKIP `validated` plans. They stay in `OPEN_STATUSES` for the uniqueness
indexes. The race is tested.

**Rollback semantics** (supersede §1.8 / §2):
- Rollback is an inverse splice of OUR links on the CMS's CURRENT HTML, done under the CMS's
  own lock. It is safe after unrelated later edits, including our own later deployments.
- It is refused only when one of our links itself changed: 409 `rollback_conflict`, with the
  plan and deployment status unchanged.
- A snapshot restore is used only when `current_version == new_version`.
- **This supersedes** the Phase 0 `cms.py` / `MockCMSProvider` docstring ("page moved →
  conflict"). The mock and simulated providers implement the new semantics, and
  `HttpCMSProvider` sends `LinkOperation.REMOVE` changes, or the TRD §13 rollback endpoint
  when the CMS supports it.
- Rolling back a `failed` or `conflict` deployment (nothing applied) is refused with
  `nothing_to_roll_back`, and the UI hides the button.
- Rollback is two-phase like apply (`rolling_back` → `rolled_back`), with reconciliation.

**Pair occupancy (amends ADR-0011's live rule).** A plan whose link is physically on the page
occupies its pair REGARDLESS of `source_page_version`, until it is `rolled_back`:
- `deployed`, `verified`, `measured` and `learning_captured`;
- `failed` when its deployment was `applied`.

The engine's `duplicate_recommendation` check uses this. The `source_page_version >= current`
window is dropped.

**Verification outcomes.** `VerificationOutcome` = `passed | failed | skipped | inconclusive`.
- Inconclusive: an HTTP network error, a 5xx, a timeout, or publish/CDN lag. The plan stays
  `deployed`; a bounded retry happens at deploy time, and `POST …/verify` re-runs on demand.
- Only a deterministic CMS-side or HTML failure moves `deployed → failed`.
- Re-verifying a `verified` deployment records a run and may move the DEPLOYMENT to
  `verification_failed`, but makes NO plan transition (`verified → failed` is not allowed).
- The deployment state machine is the one in `DeploymentStatus`. Rows are frozen in
  `failed`, `conflict` and `rolled_back`.

**Locator robustness.**
- The mutator accepts a fragment or a full document, and matches by `context_hash` only.
- Fixture tests show that, for the same page, `extract_blocks(full_rendered_html)`,
  `extract_blocks(stored_body_html)` and `extract_blocks(cms_fragment)` give the same
  content-block hashes, including a fragment without `<main>`.
- The parser is `HTMLParser(convert_charrefs=False)`. Only the span's two boundaries must map
  exactly; interior whitespace and entities are preserved verbatim.
- The self-check also asserts that every other block is unchanged, that the existing
  `<a href>` set is unchanged apart from ours, and that `remove(insert(x)) == x`.

**Identity without `data-*`.** Our link is identified by (block hash, anchor span, href).
`data-ken-link-id` is only a hint, because a CMS sanitiser may strip it. Each provider records
whether `data-*` survives. For Phase 7, attribution must not depend on the attribute: links
map to plans through (source page, anchor, href).

**Complete deploy-time checks.**
- Link density against the CMS's CURRENT HTML plus this batch (`density_exceeded`).
- The target not already linked in the CMS HTML (`already_linked`).
- CMS page resolution: the page's `external_id` for CMS-sourced pages, else the simulated
  provider's URL mapping, else `cms_page_unmapped`.
- The target fetch follows no off-host redirects (a 3xx is a failure) and reuses the Phase 1
  scope and SSRF guards.

**Dry run and operation id.**
- A dry run never records or consumes an `operation_id`.
- The deploy fingerprint is {sorted `plan_ids`, note, user}.
- A replay of an operation that ended in conflict or failure returns the stored outcome, with
  the same status code.

**Typed events.** `LinkDeployedPayload`, `LinkVerifiedPayload`, `LinkFailedPayload` and
`LinkRolledBackPayload` (`event_version` 1) are emitted in the same transaction as the status
change, with `correlation_id = operation_id`.

**Status-integrity trigger (migration 0007).**
- `AFTER UPDATE OF status … WHEN (OLD.status IS DISTINCT FROM NEW.status)`, as a DEFERRABLE
  INITIALLY DEFERRED constraint trigger.
- The migration embeds a literal transition table, with a parity test against
  `ALLOWED_TRANSITIONS`.
- A matching `link_plan_transitions` row must exist: (`plan_id`, from, to, `txid`), using a new
  `link_plan_transitions.txid xid8 DEFAULT pg_current_xact_id()` column (not `xmin`, which
  breaks under savepoints).
- The test that does a raw `UPDATE … SET status` is rewritten to expect the refusal. Tests that
  roll back use `SET CONSTRAINTS ALL IMMEDIATE`.
- Known limitation: a direct INSERT at a non-initial status (test fixtures) is not covered.

**Simulated CMS isolation.**
- `SimulatedCMSProvider` uses its OWN engine and connections and commits independently of the
  app session. It has fault-injection hooks for "CMS committed, app failed" and the reverse.
- Its storage lives OUTSIDE the app's Alembic chain: a separate database, `ken_cms_sim` on the
  project PostgreSQL, created and seeded by `make cms-sim-seed` (a disposable,
  project-local database).
- `make cms-sim-serve` binds loopback on port 0 (never 8765) and prints the verified URL.

**`HttpCMSProvider` gating.** Every network call, reads included, is refused before any I/O
unless the base URL is loopback or the user explicitly opts in to a remote base URL
(`KEN_CMS_ALLOW_REMOTE=true`). Connecting to the real CMS is an ASK action, and it is not done.
Writes additionally need `KEN_CMS_WRITES_ENABLED=true`. The token is never logged.
`config.cms_provider` widens to `mock | simulated | http`.

**Sessions.** The read-only GET session factory is used by readers only. The auth provider
keeps the read-write factory, because it writes `last_seen_at`. A test proves that a reader's
write fails.

**Reaper.** `make jobs-reap` takes the kind's lock with `pg_try_advisory_lock` before marking a
run failed, releases it afterwards, and never touches deployments.

**Optional, adopted:** per-plan rollback is a REMOVE-change deployment (an editorial reversal
of one link does not roll back the batch).

## Consequences

- The whole loop runs locally, end to end: recommend → agents → approve → deploy to the
  simulated CMS → verify over HTTP → re-crawl the simulated site → the link becomes an existing
  link → measure (Phase 7).
- Switching to the real CMS is configuration plus explicit user approval. The HTTP adapter is
  contract-tested now.

## Alternatives considered

- **Re-serialising the document after mutation.** BeautifulSoup output differs from the source
  in quoting and entities, so a real CMS would see a whole-page diff. Rejected in favour of
  splicing.
- **Letting the CMS do the mutation** (structured `LinkChange` only). It is still supported:
  the HTTP adapter sends structured changes. Our mutation is the reference behaviour, and the
  simulated CMS uses it.
- **Playwright verification.** See §6.
