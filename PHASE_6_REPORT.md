# Phase 6 Report — Controlled Deployment

**Status: complete.** Executed under the user's 2026-10-14 authorisation for autonomous sequential
delivery of Phases 2–9. Each phase has internal gates, a local commit and a `phase-N-complete` tag.
Nothing has been pushed. **No real CMS was contacted. Every mutation in this phase hit the
in-memory mock or the simulated CMS in its own disposable local database.**

**Design:**
[docs/adr/ADR-0014-controlled-deployment.md](docs/adr/ADR-0014-controlled-deployment.md).
§8 holds the binding amendments from the pre-fan-out review, §9 the integration decisions and
§10 the QA outcomes.

## 1. What was built

| Capability | Implementation |
|---|---|
| **Deployment flow** | One source page per deployment, with up to 20 approved plans. The flow is permission check → **validate** (re-checks under lock the plan status, freshness, protections, live eligibility of source and target, the existing-link state and link density) → **dry-run** preview → apply with **expected version** → **verify** → event + audit → **rollback**. Plans move `approved → validated → deployed → verified` (or `verification_failed` / `failed` / `expired` / `rolled_back`). Only the deployment service moves them. |
| **Durability and concurrency** | Two transactions. A durable `applying` intent row records the exact request before the CMS call; the outcome is recorded after it. A crash between the two is recovered by `make deployments-reconcile`, which resends the stored request under the same idempotency key. A per-source session advisory lock (`ken.deploy.source:<id>`) gives a 409 `deployment_in_progress`. `operation_id` plus a request fingerprint gives replay or a 409 when the id is reused with a different body. |
| **Deterministic HTML mutation** | `ken_services.deployment.mutation` **splices**: the output equals the input byte for byte, except the inserted `<a href="…" data-ken-link-id="…">` and `</a>`. Blocks are located with the inventory block walk, by `context_hash`. The anchor is an exact occurrence, mapped to exact source offsets with `HTMLParser(convert_charrefs=False)`, and must lie in one text node. Placement-safe and hidden-ancestor checks apply (`hidden`, `aria-hidden`, inline `display:none` / `visibility:hidden`). Our link is identified by (block hash, span, href), not by the `data-*` hint. A self-check after every splice includes `remove(insert(x)) == x`. The mutator is total: it returns issues and never raises. |
| **Href rules** | `http(s)` only; the target host must be the source host or an allowed host. No `javascript:`, `data:`, credentials, control characters, whitespace tricks or protocol-relative URLs. Hrefs are escaped. Equivalence is by the inventory's URL normalisation. |
| **Rollback** | An inverse splice on the **current** HTML, for the whole deployment or per plan, so it is safe after unrelated later edits. A changed link gives `rollback_conflict`; nothing left gives `nothing_to_roll_back`. A physically live link occupies its (source, target) pair until it is rolled back. |
| **Verification** | HTTP/HTML: fetch the published page and check that the link is present exactly once, the href, the anchor, the expected block, that it is not hidden and that the target is reachable. The outcome is `passed` / `failed` / `skipped` / **`inconclusive`**, where `inconclusive` means a transport problem and is never treated as a pass. A re-verify endpoint is provided. Browser verification is deferred (ADR-0014 §6). |
| **CMS providers** | `mock` (in memory), **`simulated`** (its own database `ken_cms_sim`, plus a loopback site from `make cms-sim-serve`) and `http` (the TRD §13 adapter, contract-tested). The `http` adapter is **refused unless the base URL is loopback or `KEN_CMS_ALLOW_REMOTE=true`, and writes also need `KEN_CMS_WRITES_ENABLED=true`**. |
| **Database integrity** | Migration `0007_controlled_deployment` adds `deployments` (frozen once finished, no delete), append-only `deployment_items` and `deployment_verifications`, and the `cms_sim` schema. It also adds **link-plan status integrity**: a deferred constraint trigger refuses any status change that is not an allowed transition or has no `link_plan_transitions` row in the same transaction (keyed on `txid xid8`). |
| **Read-only GETs** | Reader sessions run every transaction `READ ONLY`. |
| **Job reaper** | `make jobs-reap` fails `running` job runs whose kind lock no live runner holds. |
| **Events** | Typed payloads for `link.deployed`, `link.verified`, `link.failed` and `link.rolled_back`, emitted once per transition. This closes the Phase 0 item "persist the CMS audit log to `events`". |
| **API** | Seven routes. **Writes need `deploy`**, which only `operator_admin` has; reads need `read`. Routes: `POST /deployments/dry-run`, `POST /deployments`, `POST /deployments/{id}/verify`, `POST /deployments/{id}/rollback`, `GET /deployments`, `GET /deployments/{id}` and `GET /recommendations/{id}/deployments`. |
| **UI** | `/deployments` (history) and `/deployments/{id}` (items, versions, verification checks, rollback with a reason, whole or per plan). Approved plans get a **Deploy** flow (dry-run preview as text → confirm), visible only to `deploy` holders. Plan detail shows the deployment state. Operation ids are minted once per rendered form. |

## 2. Schema — migration `0007_controlled_deployment`

`deployments`, `deployment_items`, `deployment_verifications`, the `cms_sim` schema, the
deployment guard triggers, `link_plan_transitions.txid` and the `link_plans_status_integrity`
constraint trigger.

Round trip on a disposable database: head → 0006 → head is clean. `alembic check` reports
"No new upgrade operations detected". The dev DB is migrated after the phase commit.

## 3. End-to-end smoke test (live, on disposable databases)

Databases `ken_smoke6` and `ken_cms_sim_smoke6` were created for the test. The API (uvicorn)
and the simulated site each ran on a free loopback port. Three throwaway users had random
passwords; the password files were deleted afterwards.

**Setup:**
- import `tests/fixtures/demo/research_site.jsonl`: 64 pages;
- analyse: 64 embeddings, 805 relationships;
- recommend: 48 plans;
- `cms-sim-seed --export`: 64 CMS pages.

**Run:**

| Step | Result |
|---|---|
| A reviewer approves 3 plans | 200 |
| Deploy as viewer, as reviewer, anonymous | **403 / 403 / 401** |
| Dry-run as `operator_admin` | 200, `valid=true`, CMS version token, the change preview |
| Deploy 2 plans on one source page | 200 → **`verified`**, with versions `sim-seed-… → sim-…`; HTTP verification ran 7 checks per plan (present once, href, anchor, block, not hidden, not nested, target reachable): run `passed` |
| Replay the same operation id | 200, `idempotent_replay=true` |
| The same id with a different body | 409 `conflict` |
| Redeploy the same plans with a new id | 409 `conflict` (`plan_not_approved` ×2) |
| Unknown plan | 404 |
| The served page | 2 `data-ken-link-id` links; both plans `verified` |
| Roll back one plan | 1 link left; the deployment is still `verified` |
| Roll back the rest | `rolled_back`; 0 links; **the CMS body is byte-identical to the original export (2,185 bytes)** |
| Roll back again | 422 `validation_failed` (`nothing_to_roll_back`) |
| Events | `link.approved`, `link.deployed`, `link.verified`, `link.rolled_back` |
| API log | no password and no bearer token |

**Web** (production build, `next start` on a free loopback port, against the same API):
- anonymous `/deployments` → 307 to `/login`;
- `/deployments` and `/deployments/{id}` render for an admin and for a viewer;
- on a live deployment, the **admin sees the Roll back and Re-verify controls and the viewer sees neither**;
- plan detail links to its deployment;
- the CSP header is present, and the session token appears in no page.

## 4. Dependencies

**None added**, in either Python or Node. HTML work uses only stdlib `html.parser`, and the
HTTP probe and adapter use the existing `httpx`.

## 5. Verification

| Gate | Result |
|---|---|
| `make verify` (integrated code, before QA) | ruff clean, mypy clean (323 files), **3,824** Python tests passed, **1,067** web tests passed (37 files), web lint clean, compose ×3 OK, migrations at head, doctor **0 FAIL / 0 WARN** (21 checks) |
| `make verify` (final code, after QA fixes) | ruff clean, mypy clean (324 files), **3,845** Python tests passed, **1,077** web tests passed, web lint clean, compose ×3 OK, migrations at head, doctor **0 FAIL / 0 WARN** |
| Migration round trip | §2 |
| Live smoke test | §3 |

## 6. Reviews

**Pre-fan-out architecture review:** its binding amendments are ADR-0014 §8, all applied before
fan-out (commit `5560acf`):
- the durable intent and reconcile;
- the deploy lock;
- rollback semantics;
- link occupancy;
- `inconclusive` verification;
- typed events;
- HTTP-adapter gating.

**QA/security review: PASS WITH REQUIRED FIXES.** No critical or high finding. QA verified by execution:
- **Fuzzing:** an independent differential fuzz over 9,000 documents, with 0 raises and 0 broken invariants (byte-exact round trip, idempotent re-insert, verifier agreement).
- **Adversarial HTML:** 37 cases covering entities, CRLF, NUL, non-BMP characters, comments, CDATA, `plaintext`, `template`, `noscript`, hidden markers and 5,000-deep nesting.
- **HTTP-adapter gating:** 26 bypass attempts, all refused before any I/O. They included decimal, hex and short IPv4, `localhost.evil.com`, `nip.io`, userinfo, `\@`, `%2f@`, CRLF, IPv6 zone ids, Unicode lookalikes and `file://`.
- **Events:** exactly one event per transition across re-verifies and rollbacks.
- **Other:** the full RBAC matrix, the migration round trip, and no new dependencies.

| Finding | Fix |
|---|---|
| M1. A rollback form reused its operation id after a per-plan success, so the next rollback got a 409. | A fresh id after each success or recorded refusal; retries of one submission keep it; tests. |
| M2. A deploy/rollback 503 claimed that "nothing was changed", but the outcome may be unknown after the durable intent. | The copy says the outcome is unconfirmed and that resubmitting replays safely; data is refreshed; tests. |
| L1. A refused rollback's id replayed as a fresh attempt. | The refusal is recorded (`rollback_conflict` items) and replayed identically; no migration; test. |
| L2. Parser differential (`<script>` with `<!--<script`). | The document is unsound: mutation refuses, verification fails; tests. |
| L3. `reconcile` against the in-process mock. | Refused (exit 2); test. |
| L4. The mutator and verifier blocked the event loop. | `asyncio.to_thread`. The lock scope across network checks is a known limitation. |
| L5. Hard-coded sim test DB. | `KEN_TEST_CMS_SIM_DATABASE_URL`, guarded to `ken_cms_sim*_test`. |
| L6. A flaky analysis test. | Root cause: a random uuid4 sometimes picked a no-op page. Fixed without weakening the assertion. |
| I1–I4 | Informational, accepted (ADR-0014 §10). |

## 7. Incidents

- The first deployment-backend specialist died with an API error mid-task. A replacement
  re-read the frozen contracts, re-checked the partial work and completed it.
- The mutation specialist ran `ruff format` on two files it did not own. The change was
  formatting only and was reviewed.
- No real CMS and no external network were touched. Every specialist and QA run used its own
  test database.

## 8. Known gaps

See NEXT_STEPS.md, "Phase 6 known limitations". The main ones:
- parity with Ken's real templates is unproven;
- browser verification is deferred (this amends TRD §2 / PRD §7.11 and is listed for your
  decision);
- a least-privilege DB role is documented, but no role was created;
- `events` is not yet append-only in the database;
- the HTTP-CMS 409 ambiguity.

## 9. Information still needed from you

- **Real CMS integration** (ASK): the CMS API base URL, its link-change contract (how it
  distinguishes a version conflict from a reused operation id), and **write credentials** stored
  outside source control. None was used.
- **Browser verification:** confirm the deferral of Playwright (ADR-0014 §6), or ask for it to
  be added.
- Still open: create your accounts; the self-describing-anchor rule (ADR-0011 §9.1); and whether
  a model critic's reject filters plans out of the queue.

## 10. Commit

`feat: complete Phase 6 Controlled Deployment`, tag `phase-6-complete` (local only).
