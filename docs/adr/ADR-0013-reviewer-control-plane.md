# ADR-0013 — Phase 5 reviewer control plane: local auth, roles, human review, feedback

- **Status:** Accepted (Phase 5); amended before fan-out by the pre-fan-out architecture review (§7)
- **Date:** 2026-10-14
- **Deciders:** orchestrator (under the user's 2026-10-14 autonomous-delivery authorisation,
  which lists "secure local auth provider with an abstraction; roles viewer / reviewer /
  operator-admin; AUTO_APPROVE OFF; Electron portability preserved" as Phase 5 scope)
- **Related:** PRD §§3, 7.9; TRD §§14, 16, 20, 23; ADR-0008 (Electron readiness), ADR-0011,
  ADR-0012

## Context

Phases 0–4 expose a GET-only API with no authentication. Phase 5 adds the first human
mutations: approve, reject, edit anchor or placement, replace the destination, and bulk
review. So it must add authentication, RBAC (TRD §23) and an audit and feedback record. The
system is local-first and must stay free and self-hosted, and it must remain Electron-portable.

## Decision

### 1. Authentication: a provider abstraction with one local, self-hosted provider

- **`AuthProvider` protocol** (`ken_contracts.auth`): `login`, `authenticate` and `logout`.
  The first implementation is `LocalAuthProvider` over PostgreSQL. An OIDC or SSO provider
  can be added behind the same protocol later.
- **Passwords:** `hashlib.scrypt` from the standard library (n = 2¹⁷, r = 8, p = 1, a 16-byte
  random salt; §7), stored as `scrypt$<n>$<r>$<p>$<salt>$<hash>`. Verification is constant-time.
  There is no new dependency. Passwords are 12–256 characters.
- **Lockout:** 5 consecutive failures lock an account for 15 minutes. Every failure looks the
  same to the caller (`InvalidCredentialsError`), so the response never reveals whether a
  user exists. Unknown users still pay the hash cost, to equalise timing.
- **Sessions:**
  - the token is an opaque 256-bit `secrets.token_urlsafe(32)`; only its SHA-256 is stored;
  - sessions last 12 hours, can be revoked at logout, and record `last_seen_at`;
  - there is no JWT, so revocation is immediate.
- **No default credentials.** Users are created only through the CLI:
  `make user-create USERNAME=… ROLE=…`, which prompts for the password or reads it from
  stdin. No account exists after a fresh install. The demo seed (end-to-end phase) creates
  accounts with passwords taken from environment variables, never committed.

### 2. Transport: Bearer at the API, an HttpOnly cookie in the web app

- **The API** accepts only `Authorization: Bearer <token>`. It has no cookies, so it carries
  no ambient authority and has no CSRF surface.
- **The web app** (Next.js, server-rendered) logs in through a route handler that calls
  `POST /auth/login`. It stores the token in its own `HttpOnly; SameSite=Strict; Path=/`
  cookie, adding `Secure` outside local development. Server components and server actions
  read the cookie and forward the token to the API.
- **Mutations in the web app** are Next server actions, which Next protects with its
  same-origin check. Browser JavaScript never sees the token.
- **Electron** loads the same web app, which keeps ADR-0008 intact: no Electron import and
  no token in renderer-accessible storage.

### 3. Authorisation: three roles, one permission matrix

| Role | Permissions |
|---|---|
| `viewer` | `read` |
| `reviewer` | `read`, `review` |
| `operator_admin` | `read`, `review`, `read_audit`, `read_settings` |

- Every route declares the permission it needs. Only `/health` and `/ready` (liveness and
  readiness probes) and `POST /auth/login` are public. `/meta` requires `read`.
- No role carries "approve automatically". AUTO_APPROVE stays globally disabled (TRD §20,
  Settings validator).
- Phase 5 adds no endpoint that changes settings, users, policies or secrets. User
  administration is CLI-only, and settings and policies are **read-only** views. Changing
  authentication or secrets settings remains an ASK action (CLAUDE.md).
- **Actors** are derived from the session (`Principal.actor = "user:<username>"`), never from a
  request body. This closes the Phase 3 QA H3 gate: `TRANSITION_AUTHORITY` checks the class,
  and the API guarantees the identity.

### 4. Human review (PRD §7.9)

All review runs through the `ReviewService` (`ken_contracts.review`), a deterministic service
over the Phase 3 transition service. Every action is one database transaction.

| Action | From | Path |
|---|---|---|
| approve | proposed / critiqued / policy_checked / awaiting_approval | `→ awaiting_approval →` (when needed) `approved`, by the user |
| reject | the above + approved (reversal) | `→ rejected`, by the user; ≥ 1 structured reason |
| edit anchor / placement | as approve | old `→ superseded` (user); NEW plan inserted (`anchor_source = "edited"`, initial row by `system:review-service`, note naming the user) `→ awaiting_approval` (user) [`→ approved` if `approve = true`] |
| replace destination | as approve | as edit, same anchor span, a new target |
| bulk approve / reject | `awaiting_approval` only, not stale | ≤ 50 plans, each in its own transaction, per-plan results; edits are never bulk |

**Invariants:**
- **Idempotent.** A client-generated `operation_id` is UNIQUE in `review_actions`. A replay
  returns the stored result (`idempotent_replay = true`). The same id with a different
  request fingerprint is a 409.
- **Version-aware.**
  - `expected_status` must match the plan's status;
  - a STALE plan (`check_plan_freshness`) cannot be approved or edited: 409
    `stale_page_version`;
  - an edit carries the `block_context_hash` the reviewer saw.
- **Verbatim, never fabricated.**
  - An edited anchor is `block.text[start:end]` of the plan's block in the source's
    **current** version.
  - It must pass `span_is_safe`, the anchor rules, and no-overlap with other open plans' spans
    and existing links.
  - A replaced target must pass the pair filters: eligible, unprotected, not already linked,
    no other open plan for the pair, not the source.
- **Auditable.**
  - Every action appends one `review_actions` row: actor, user id, from and to status,
    reasons, note, anchor and target before and after, and the bulk operation id. This row is
    also the **editorial feedback** record.
  - Every action emits `link.approved` or `link.rejected`, and an edit emits `link.proposed`
    for the new plan, each with the actor.
- **Reversible** until deployment (Phase 6): `approved → rejected`. A supersede keeps the old
  plan and its history.

### 5. The control plane (web)

- a dashboard;
- inventory and intelligence (existing);
- the recommendation inbox (`awaiting_approval` by default) and plan detail with a
  side-by-side before/after preview, rendered as text with the anchor highlighted (never as
  HTML);
- approve, reject, edit-anchor (span picker over the block text), replace-destination (a
  picker of eligible targets) and bulk-select forms;
- agent and job run history (existing);
- the audit log (review actions plus the event log, for `operator_admin`);
- system health (existing `/system`);
- read-only settings and policy views (`operator_admin`).

Controls a role may not use are not rendered, and the API enforces the permissions anyway.
Deployment history arrives with Phase 6, which produces its data.

### 6. Hardening carried in from earlier phases

- 401 maps to `unauthenticated` and 403 to `permission_denied`, through new `ErrorCode`
  members; 405 maps to `method_not_allowed` (Phase 0 QA L1).
- `page_protections` gets a trigger rejecting DELETE and non-deactivating UPDATEs (§7).

### 7. Amendments from the pre-fan-out architecture review (binding for implementation)

**Routes and permissions.** Deny by default: router-level dependencies, and an explicit
public allowlist. A route-introspection test asserts every route is public or declares a
permission. A real-auth matrix test asserts 401 without a token and 403 below the required
role, for every route. There is no settings flag that disables auth. Tests use a principal
only through `app.dependency_overrides` (`make_app`).

| Permission | Routes |
|---|---|
| public | `GET /health`, `GET /ready`, `POST /auth/login`; `/docs` and `/openapi.json` only when `environment` is `local` or `ci` |
| any session | `GET /auth/me`, `POST /auth/logout` |
| `read` | `/meta`, `/inventory/*`, `/intelligence/*`, `/jobs/*`, `/recommendations/*` (GET), `/agents/*`, `GET /recommendations/{id}/review-actions`, `GET /dashboard` |
| `review` | `POST /recommendations/{id}/approve`, `/reject`, `/edit-anchor`, `/replace-target`; `POST /recommendations/bulk-review`; `GET /recommendations/{id}/target-options` (eligible replacement targets) |
| `read_audit` | `GET /review/actions` (all actions, filters), `GET /events` (the event log, paged) |
| `read_settings` | `GET /settings/policies`: the four active policy documents, parsed and validated, with versions and paths. `/meta` stays the minimal public-settings view. |

**Deviation from TRD §14.** TRD §14 names `PATCH /api/recommendations/{id}` for edits. Edits
here never mutate a plan; they supersede it. They are therefore explicit POST actions
(`/edit-anchor`, `/replace-target`), and this is recorded as a deliberate deviation. Approve
and reject follow TRD §14.

**Review service.** It takes a `Principal`, not an actor string. It derives `actor` and
`user_id` itself, refuses a non-`user:` principal, and asserts `Permission.REVIEW` again as
defence in depth.

**Idempotency.**
- `request_fingerprint` = sha256 of canonical JSON (sorted keys) of
  `{action, plan_id, user_id, body}`.
- A unique violation on `operation_id` (a concurrent duplicate) is handled by rolling back,
  re-reading the stored row, and returning its replay, or a 409 if the fingerprint differs.
- Refused actions (409 or 422) write NO row, so a retry re-executes.
- **Bulk:**
  - each item's row has `operation_id = uuid5(bulk_operation_id, str(plan_id))`, a
    `bulk_operation_id`, and the BULK fingerprint (`{action, sorted plan_ids, user_id, body}`);
  - reusing a bulk id with a different action or plan set is a 409;
  - on replay, done items return their stored result and failed items (which have no row)
    are re-attempted;
  - `idempotent_replay` is true only if every item replayed;
  - events use `correlation_id = bulk_operation_id`.

**Re-validation under the row lock.** Approve, bulk approve and edits lock the plan row and
re-run two checks, failing closed with a 409:
- `check_plan_freshness` (stale → `stale_page_version`);
- the durable-invalidity rules of the engine: source or target protected, gone, unpublished,
  noindex, canonical alias, thin or an excluded page type, or the pair already linked in the
  current version.

The invalidity logic is **extracted** from the pipeline into one shared deterministic function
(`ken_services.recommendations.validity`) that both use; it is never duplicated.

**Serialised writers per source.** Both the pipeline's per-source transaction and review
edits take `pg_advisory_xact_lock(hash("ken.plans.source:" + source_page_id))`. Inside one
edit transaction the old plan is superseded BEFORE the new one is inserted, because the
open-pair and open-span unique indexes are not deferrable; this is tested for both edit kinds.
A residual unique violation maps to 409 `conflict`.

**Cool-down after replace_target.** The engine's `previously_rejected` cool-down also counts a
target that a reviewer replaced, joined through `review_actions.action = 'replace_target'`.
Without this, the next `make recommend` would re-propose the destination the reviewer just
replaced. Tested.

**How an edited plan is built.** It is a fresh `LinkPlan`, never a copy of another target's
facts:

| Field | Value |
|---|---|
| `source_page_version` | the source's CURRENT version; the plan must be fresh or rebasable |
| `block_id` | the CURRENT block whose text hash equals `block_context_hash`, which must equal the old plan's `context_hash` |
| `context_hash` | as above |
| `target_page_version` | the target's current version |
| `supersedes` | the old plan |
| `run_id` | `None` |
| `anchor_source` | `edited` |
| `anchor_text` | the verbatim slice |
| locator, preview | recomputed |
| scoring | recomputed deterministically with the CURRENT policies. `edit_anchor`: `with_placement` over the old pair's relevance, with placement quality for an edited anchor. `replace_target`: `score_pair` for the NEW pair, then `with_placement` |
| `channels`, `relationship_type`, `reason_codes` | only what was computed for that pair; `channels` is empty for a pair no channel retrieved |
| `rationale` | rendered from those codes |
| `engine_version`, `policy_version`, `ranking_policy_version`, `analysis_version` | current |
| `calibration_version` | `None` |

**Events.**
- approve → `link.approved`;
- reject or reversal → `link.rejected`;
- edit → `link.proposed` for the NEW plan, with `LinkPlanEvent.supersedes` set (the old
  plan's `superseded` status is in its transition history); with `approve = true`, also
  `link.approved` for the new plan.

Every event carries the user as `actor` and `correlation_id = operation_id`.

**Auth hardening.**
- scrypt N = 2¹⁷, r = 8, p = 1, `maxmem` = 192 MiB, which meets the OWASP minimum. A stored
  hash with lower parameters is re-hashed at the next successful login.
- scrypt runs in `asyncio.to_thread` behind a semaphore (2), to resist memory and event-loop
  denial of service.
- A password is ALWAYS verified first, against a fixed dummy hash for unknown users, and only
  then are disabled or locked states checked.
- A locked account returns the SAME 401 body as invalid credentials, with no Retry-After.
- `failed_logins` is incremented atomically (`UPDATE … RETURNING`).
- `authenticate` re-reads `role` and `disabled` on every request. A CLI disable, role change
  or password change revokes the user's sessions.
- The Authorization header, the token and the password are never logged; a redaction test
  covers this.
- `last_seen_at` is written at most once a minute. `auth_sessions` UPDATE may change only
  `revoked_at` and `last_seen_at` (trigger); expired sessions are pruned by a CLI.
- Per-account lockout lets someone who knows a username lock a reviewer out for 15 minutes.
  This is accepted for a few local users.
- **Web:**
  - login is a server action, with Next's origin check, and so is logout;
  - one exempt server module reads the cookie and forwards the token, so no page can forget
    to;
  - cookie Max-Age equals the session's remaining TTL;
  - an API 401 clears the cookie and redirects to `/login`;
  - authorisation is never decided in Next middleware;
  - a Content-Security-Policy is set.

**Credentials are never invented.** The orchestrator creates no account with a password it
chose. The demo seed creates accounts only from passwords the user supplies through
environment variables, and otherwise prints the `make user-create` instructions.

**Phase-5-targeted items from NEXT_STEPS:**

| Item | Decision |
|---|---|
| Read-only DB path for GET handlers | **re-deferred to Phase 6**, together with the least-privilege role and the SECURITY DEFINER status-integrity item, as one database-role design |
| Agent-run trigger | stays **CLI-only** (no run-start endpoint) |
| `identity_conflict` operator tooling | **re-deferred**; operator CLI with the Phase 6 operator tooling |
| 422 echoing a malformed-UUID character | **fixed in Phase 5**: validation errors no longer echo input values |
| `page_protections` trigger | **done in 0006**: rejects DELETE, and any UPDATE other than deactivation (`active` true → false plus its deactivation fields) |

**Document amendments.**
- CONTRACTS §16 and ADR-0012 now read "the engine produces plans; the review service
  produces only the superseding plan of a human edit".
- ADR-0008: the HttpOnly-cookie model makes the desktop shell load the local Next server
  (option 1). Client-side fetching (option 2) would need its own token-custody design.
- TRD §5's `link_reviews` table is `review_actions`.

**Placement scope.** An edit picks a new span in the SAME block. Moving a link to another
block is done by rejecting the plan with `wrong_placement`; the engine places by its own
rules. This is a deliberate limit, so that the editing UI never builds a locator across
blocks.

## Consequences

- From Phase 5, every non-probe endpoint requires a session. Tests use an authenticated test
  principal by default (`make_app`) and real auth in the auth tests.
- The review queue is the place where human decisions are recorded with structured reasons.
  Phase 8 learns from `review_actions`, never from free text alone.

## Alternatives considered

- **JWT.** Stateless, but revocation is hard and it adds a dependency. Opaque server-side
  sessions are simpler and revocable.
- **Cookies at the API.** These would require CSRF tokens and SameSite reasoning at two
  origins. A Bearer API with a same-origin cookie in the web tier is simpler, and portable
  to Electron.
- **argon2 / bcrypt.** Better tuned, but each is a new native dependency. `scrypt` (stdlib) is
  memory-hard and sufficient for a local, few-user deployment; switching later is a
  hash-scheme prefix change.
- **An external IdP** (Keycloak and similar). That is new infrastructure, an ASK action. The
  provider abstraction leaves the door open.
