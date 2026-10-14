# Phase 5 Report — Reviewer Control Plane

**Status: complete.** Executed under the user's 2026-10-14 authorisation for autonomous sequential
delivery of Phases 2–9. Each phase has internal gates, a local commit and a `phase-N-complete` tag.
Nothing has been pushed.

**Design:**
[docs/adr/ADR-0013-reviewer-control-plane.md](docs/adr/ADR-0013-reviewer-control-plane.md).
§7 holds the binding amendments from the pre-fan-out review; §8 holds the QA outcomes.

## 1. What was built

| Capability | Implementation |
|---|---|
| **Local auth** | An `AuthProvider` abstraction with a `LocalAuthProvider`. Users live in PostgreSQL. Passwords use stdlib `scrypt` (N = 2¹⁷, r = 8, p = 1), run off the event loop behind a semaphore and re-hashed on login when the parameters are older. Every failure path (unknown user, wrong password, disabled, locked) costs the same and returns the same 401. Five failures lock the account for 15 minutes. Sessions are opaque 256-bit Bearer tokens stored as SHA-256, last 12 h and can be revoked; role and disabled state are re-read on every request. **No default account.** |
| **User admin (CLI only)** | `make user-create / user-list / user-disable / user-enable / user-set-role / user-set-password / sessions-prune`. Every change revokes the user's sessions. |
| **RBAC** | Roles `viewer` < `reviewer` < `operator_admin`; permissions `read` / `review` / `read_audit` / `read_settings`. Deny by default: only `/health`, `/ready` and `POST /auth/login` are public. A route-introspection test and a real-auth 401/403 matrix over every route enforce it. No setting disables auth. |
| **Human review** | `ReviewService`: approve (via `awaiting_approval` when needed), reject (or reverse an approval), edit anchor or placement, replace destination, and bulk approve/reject (≤ 50). The actor always comes from the session. It is idempotent (`operation_id` plus a request fingerprint, including concurrent duplicates and bulk replay) and version-aware (`expected_status`, freshness, `block_context_hash`). It re-validates durable invalidity under the plan lock, using `validity.py`, which is now shared with the engine. Writers are serialised per source with an advisory lock. Edits supersede the plan: the anchor is a verbatim, whole-phrase, placement-safe span; the score is recomputed; the destination must pass the pair filters. |
| **Feedback and audit** | Append-only `review_actions`, one row per action. It records the action, actor, user, status before and after, structured reasons (`REASONS_BY_ACTION`), note, anchor and target before and after, the new plan id and status, and the bulk id. Actions emit `link.approved`, `link.rejected` and `link.proposed` (with `supersedes`). A destination a reviewer replaced is never re-proposed within the cool-down. |
| **Control-plane reads** | `GET /dashboard`, `/events` (audit), `/review/actions` (audit), `/settings/policies` (validated policy documents with sha256), `/recommendations/{id}/review-actions` and `/target-options`. |
| **UI** | `/login`, and a role-aware header and navigation. The dashboard shows "AUTO_APPROVE: Disabled". The inbox defaults to `awaiting_approval` and offers bulk review. Plan detail has a before/after preview, approve and reject with structured reasons, an edit-anchor span picker that works by keyboard and counts code points, replace destination, the review history and the agent review. Also `/audit/reviews`, `/audit/events` and `/settings/policies`. The token sits in an HttpOnly, SameSite=Strict cookie and is read only in `lib/api.ts`. A 401 goes to `/auth/expired`, then `/login`. A CSP is set, and all model and user text renders as text. |
| **Hardening carried in** | 401 → `unauthenticated`, 403 → `permission_denied`, 405 → `method_not_allowed`. Validation errors never echo input. A `page_protections` trigger allows deactivation only. `users` and `auth_sessions` have column-guard triggers, and revocation is final. |

## 2. Schema — migration `0006_review_control_plane`

Tables: `users`, `auth_sessions` and append-only `review_actions`. The `page_protections`
history guard is added. All CHECKs, foreign keys and indexes match the ORM, and
`alembic check` shows no drift. The round trip head → 0005 → head is clean. The dev DB is
migrated.

## 3. End-to-end smoke test (live, on a disposable test DB)

**API** (uvicorn on a free local port), with three throwaway users whose random passwords were
never stored:
- a request without a token → 401;
- a wrong password and an unknown user return identical bodies;
- a viewer approving → 403;
- a reviewer approval → 200; replaying it gives `idempotent_replay = true`; its id reused
  for a reject → 409; the reversal → `rejected`;
- events as a reviewer → 403, as an admin → 200;
- all four policies are served, with `auto_approve_enabled = false`;
- logout → 204, and the token then gets 401;
- neither the password nor the token appears in the API log.

**Web** (production build):
- anonymous `/` → 307 to `/login`; the reviewer's dashboard renders;
- a viewer sees no Approve control, and a reviewer sees the audit pages refused;
- an admin sees events and policies; the CSP header is present;
- a bogus cookie → `/auth/expired`.

**Full chain:** recommend → agents handed off 47 plans → the inbox shows bulk review → a bulk
approve of 3 plans succeeds.

## 4. Dependencies

**None added.** Python uses stdlib `scrypt` and `secrets`. The web app uses Next/React
built-ins. There is no new service.

## 5. Verification (final run, on the final code)

| Gate | Result |
|---|---|
| `make verify` (final code) | ruff clean, mypy clean (278 files), **2,992** Python tests passed (the first run had 1 failure: the OpenAPI snapshot, regenerated for the new query patterns, then 211/211 contract tests), **951** web tests passed, web lint clean, compose ×3 OK, migrations at head, doctor **0 FAIL / 0 WARN** |
| QA fixes | covered by new tests (M1, L1, L2, L3) and code changes (L4, L6, L7) |
| Live smoke test | §3 |

## 6. Reviews

**Pre-fan-out architecture review: approved with 13 required changes, all applied before
fan-out:**
1. a principal-based service;
2. a precise fingerprint and bulk idempotency;
3. durable re-validation under lock, from an extracted `validity.py`;
4. per-source advisory locks;
5. the cool-down after `replace_target`;
6. how an edited plan is built, field by field;
7. `LinkPlanEvent.supersedes`;
8. auth hardening (scrypt parameters, a thread semaphore, a dummy hash, an identical lockout
   401, atomic counters, re-reads, redaction, server-action login);
9. deny-by-default RBAC with a route table;
10. the TRD §14 deviation recorded;
11. the NEXT_STEPS deferrals decided;
12. document amendments (ADR-0008, ADR-0012, CONTRACTS, TRD §5 naming);
13. the API surface frozen early, so the frontend and backend ran in parallel.

**QA/security review: PASS WITH REQUIRED FIXES.** It found no critical or high issue. The
timing probe showed no oracle: unknown users 278.6 ms, wrong password 279.4 ms, disabled
271.6 ms. A 12-way concurrent lockout probe stayed consistent, and the token appears nowhere in
the built pages or RSC payloads.

| Finding | Fix |
|---|---|
| M1. `safeNextPath` returned `//evil.com` for `/..//evil.com`, after normalisation. Not reachable, because it was re-sanitised later. | Re-check after normalisation; tests. |
| L1. NUL or a lone surrogate gave a 500 (login, audit queries). | A shared `reject_unstorable_text` and a query pattern give a 422; tests. |
| L2. A bulk id reused concurrently with a different plan set ran twice. | A session advisory lock per bulk id; concurrency test. |
| L3. A loopback Host dropped the Secure flag in production. | The exception now applies in development only; tests. |
| L4. `lib/api.ts` had no runtime server-only guard. | It throws if loaded in a browser. |
| L6. The web app minted a new operation id per invocation, so a retry got a 409 instead of a replay. | Single-plan forms mint the id once per rendered form. |
| L7. Rich tracebacks could render frame locals. | `plain_traceback` pinned. |
| L5. Malformed JSON gets a 422 before authentication. | Accepted and documented (ADR-0013 §8). |
| Info. `unsafe-inline` CSP; `/docs` visible when `environment = local` by default. | NEXT_STEPS. |

## 7. Incidents

None. Every specialist and QA run used its own test database. Smoke-test users existed only on
a disposable test DB; their password files were deleted after the run.

## 8. Known gaps

See NEXT_STEPS.md, "Phase 5 known limitations":
- per-account lockout DoS;
- the `unsafe-inline` CSP;
- review forms need JavaScript;
- `target-options` does not scale to a large site.

Re-deferred to Phase 6: the read-only and least-privilege DB role, database-level status
integrity, and operator tooling for `identity_conflict`.

## 9. Information still needed from you

- **Create your accounts:** `make user-create USERNAME=… ROLE=operator_admin`. None exists,
  and none was created for you.
- Still open: the self-describing-anchor rule (ADR-0011 §9.1) and whether a model critic's
  reject filters plans out of the queue (Phase 4).

## 10. Commit

`feat: complete Phase 5 Reviewer Control Plane`, tag `phase-5-complete` (local only).
