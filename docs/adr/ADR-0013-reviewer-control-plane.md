# ADR-0013 — Phase 5 reviewer control plane: local auth, roles, human review, feedback

- **Status:** Accepted (Phase 5)
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
- **Passwords:** `hashlib.scrypt` from the standard library (n = 2¹⁴, r = 8, p = 1, a 16-byte
  random salt), stored as `scrypt$<n>$<r>$<p>$<salt>$<hash>`. Verification is constant-time.
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
- `page_protections` gets a DELETE trigger, as Phase 2 planned for Phase 5.

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
