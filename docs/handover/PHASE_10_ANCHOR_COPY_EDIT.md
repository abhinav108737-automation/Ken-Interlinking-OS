# Phase 10 plan and handover: "market" anchor copy edits

- **Status:** DRAFT for owner approval, 2026-10-29. Nothing here is implemented.
- **Authority:** [ADR-0020](../adr/ADR-0020-anchor-copy-edits.md) (accepted for planning).
- **Grounding:** written after reading `contracts/python/ken_contracts/recommendations.py`
  (`LinkPlan`), `services/ken_services/deployment/changes.py`, the `recommendations/`, `review/`
  and `autonomy/` package listings and the ADRs. File names below are real; behaviour I have not
  traced is marked **(verify)**.

---

## 1. Phase plan

### Goal
When a link plan targets a market page, let the system propose inserting the single word
"market" right after the anchor ("auto parts" -> "auto parts market"). Only an `operator_admin`
can approve it. It publishes through the CMS adapter and rolls back to the exact original text.

### Out of scope
- Inserting any word other than "market".
- Copying sentences or statistics from the target report into the source page (owner decision 4;
  would need its own ADR).
- Autonomous publishing of this plan kind.
- Changing how ordinary `link` plans work.

### Tasks

| # | Task | Where (real files) | Acceptance |
|---|---|---|---|
| 1 | Alembic migration: add `plan_kind` (`link` default, `anchor_copy_edit`), `original_block_text`, `edited_block_text`, `inserted_word` to link plans; backfill existing rows as `link` | `migrations/`, `link_plans` table | Upgrade and downgrade pass; existing 48 demo plans unchanged |
| 2 | Contract: extend `LinkPlan` with the new fields; keep `anchor_source` verbatim rule for `link` | `contracts/python/ken_contracts/recommendations.py:604` | Frozen-contract tests updated; generated API types regenerated |
| 3 | Proposal step: deterministic rule that, for a market-page target and an anchor not already followed by "market", proposes the edit; reuse `placement.py` for the span | `services/ken_services/recommendations/placement.py`, `pipeline.py`, `filters.py` | Only "market" is ever inserted; never inside a link, table, heading or protected block |
| 4 | Invariant check: token multiset outside the insertion is unchanged (numbers, names, dates, claims) | `recommendations/validity.py` | Fails closed on any difference |
| 5 | Approval rule: `approve` of this kind requires `operator_admin`; reviewer and viewer get a 403 | `services/ken_services/review/service.py`, `controlplane.py`, `ken_contracts/auth.py` `Role` | Test per role |
| 6 | Exclude the kind from autonomy: never decided, never auto-published | `services/ken_services/autonomy/engine.py` **(verify where plans are selected)** | Autonomy sweep skips it in every mode |
| 7 | Deployment: generalise the splice from "insert one `<a>`" to "replace one block span", version-locked and idempotent | `deployment/changes.py`, `deployment/protocol.py` (`LinkInsertion`, `LinkRemoval`), `ken_contracts/cms.py` (`LinkOperation`) | Every other byte identical; self-check passes |
| 8 | Verification after publish: the edited text and the link are both present | `deployment/verification.py` | Typed result recorded |
| 9 | Rollback restores `original_block_text` exactly, not only the link | `deployment/mutation.py`, `changes.py` (`removal_for`) | Round-trip test: apply then rollback gives the original HTML byte for byte |
| 10 | New deployment issue codes (text changed on the page since planning, text edit conflict) | `ken_contracts/deployment.py` (`DeploymentIssueCode`) | Surfaced in the UI |
| 11 | Frontend: before/after text diff on the plan page; hide the approve action unless the user is `operator_admin` | `apps/web/app/recommendations/[planId]/page.tsx`, `components/recommendations/` | Component tests |
| 12 | Audit events for every step of this kind | existing typed-event mechanism **(verify)** | Events visible on `/audit` |
| 13 | `recommendation.v2` policy file (v1 stays immutable) holding the switch and limits | `policies/recommendation/` | Validated on load |
| 14 | Evaluation fixtures (section 3) and a no-write shadow pass | `evals/`, `tests/fixtures/` | Shadow report reviewed by the owner |
| 15 | Tests for every new endpoint; lint and type checks clean | `tests/` | CI green |
| 16 | Human promotion of the v2 policy | `make policy-promote` flow | Owner action, not an agent |

### Order
1 -> 2 -> (3, 4) -> 5 -> 6 -> 7 -> (8, 9, 10) -> 11 -> 12 -> 13 -> 14 -> 15 -> 16.
Tasks 11 and 12 can start once task 2 lands.

### Risks
- Changes published copy; Ken Research's editorial process may require a human editor (owner to confirm).
- The claim that keyword-rich anchors help search is a hypothesis, not evidence in this repo.
- Evidence so far: 5 reviews, one reviewer, demo data.
- A bug in task 7 or 9 alters live content; hence no autonomy and exact rollback.

---

## 2. Contract draft (proposed; tech to confirm against the real schema)

**New `LinkPlan` fields**

| Field | Type | Notes |
|---|---|---|
| `plan_kind` | `"link" \| "anchor_copy_edit"` | default `"link"` |
| `inserted_word` | `"market" \| null` | null for `link` |
| `original_block_text` | `str \| null` | full block text before the edit |
| `edited_block_text` | `str \| null` | full block text after the edit |
| `insertion_offset` | `int \| null` | offset in the normalised block text |

**Invariants for `anchor_copy_edit`**
- `edited_block_text` equals `original_block_text` with `inserted_word` plus one space inserted at `insertion_offset`, and nothing else.
- The new anchor text is the original anchor followed by " market".
- `anchor_source` verbatim rule is replaced for this kind by the rule above.

**Approval**
- `POST` review action `approve` on this kind: allowed for `operator_admin` only; otherwise a 403 with a fixed message. (Existing roles: viewer, reviewer, operator_admin.)

**CMS contract**
- Add a change operation that replaces one block span and inserts the link in the same step. `LinkChange` currently carries `context_hash`, `occurrence_index`, `anchor_text`, `href` **(verify whether `LinkOperation` can be extended without breaking the frozen contract)**.

---

## 3. Evaluation fixture list

Each case: a source block, target, and the expected result.

**Must propose**
1. Anchor "auto parts", target a market page, next word is not "market" -> propose "auto parts market".
2. Anchor "rooftop solar" -> "rooftop solar market".
3. Anchor in the first block vs a later block (placement rules still apply).

**Must not propose (negative)**
4. The next word is already "market" -> no edit (no "market market").
5. Target is not a market page (insight, methodology) -> no edit.
6. The anchor is inside an existing link, a heading, a table, or a hidden element -> no edit.
7. The source page is protected -> no plan.
8. The edit would change a number, a named entity or a date (injected by fixture) -> rejected by the invariant check.
9. A second insertion in the same block -> rejected (one per plan).
10. Source page version changed since planning (stale) -> invalidated, not published.
11. Block text appears twice on the page (ambiguous) -> deployment refuses.

**Approval and policy**
12. Reviewer tries to approve -> 403. Viewer -> 403. `operator_admin` -> allowed.
13. Autonomy sweep in shadow, guarded and autonomous modes -> never decides this kind.

**Deployment**
14. Apply then rollback -> original HTML byte for byte.
15. Apply twice (idempotency) -> second is a no-op.
16. Page text changed after deployment, then rollback -> `rollback_conflict`, nothing overwritten.
17. Verification fails after publish -> automatic rollback records the reason.

---

## 4. Handover note

### Run it on Windows
Source of truth: `C:\Projects\Ken-Interlinking-OS`. The OneDrive copy is stale; do not use it
(`node_modules` and `.venv` inside OneDrive are unreliable).

1. Prerequisites: Python 3.12, Node 24, Docker Desktop (WSL 2), Ollama.
2. `python -m venv .venv`, then `.venv\Scripts\pip install -e ".[dev]" -c requirements.lock.txt tzdata`.
   `tzdata` is needed on Windows or the settings fail to load.
3. `npm ci` from the repo root.
4. `docker compose up -d postgres qdrant` (Postgres on 127.0.0.1:5433).
5. In `.env`, use `127.0.0.1` not `localhost` for the database; `localhost` is slow on Windows.
6. `.venv\Scripts\alembic upgrade head`.
7. API: `.venv\Scripts\uvicorn ken_api.main:app --host 127.0.0.1 --port 8000`.
8. Web: from `apps\web`, run `..\..\node_modules\.bin\next dev --hostname 127.0.0.1 --port 3000`.
   The `npm run dev` script uses `NEXT_TELEMETRY_DISABLED=1 next dev`, which fails in Windows `cmd`.
9. Create a user: `.venv\Scripts\python -m ken_services.auth.cli user-create --username <name> --role operator_admin --display-name "<Name>"`.
   Usernames allow only lower-case letters, digits, `.`, `_`, `-` (no `@`).
10. Models: FAST and REASONING use `qwen3:4b`; embeddings use `nomic-embed-text` (both pulled in Ollama).
11. Demo data: `python -m ken_services.inventory.cli import-file tests\fixtures\demo\research_site.jsonl`, then
    `ken_services.intelligence.cli analyze`, `ken_services.recommendations.cli recommend`, `ken_agents.cli agents`.

### Known issues
- `next build` fails: pages for `autonomy/blocked/[planId]`, `learning/proposals/[proposalId]` and
  `learning/skills/[name]` are missing from this branch, and `lib/audit.ts` lacks an `autonomy` entry.
  `next dev` does not type-check, so it runs.
- Tests and linters have not been run since: the Next.js 16.4.0 -> 16.4.3 bump (fixes critical
  advisories GHSA-2464-fhr4-w9xw, GHSA-p3m5-59mp-jv5r, GHSA-673q-mjm6-r5xr), and 8 restored frontend files.
- Git shows many files as changed from Windows line endings only.
- Nothing from this session is committed.
- Services do not restart after a reboot (no run script yet).
- Data in the database is demo data (`research.example.com`) plus 3 tiny mock pages.
- The learning step needs 30 samples per decision class; 5 exist, so it proposes nothing yet.

### Project rules that apply to this work
No paid APIs; Postgres is the system of record; every schema change needs an Alembic migration;
every endpoint needs tests; every mutation idempotent, version-aware, auditable and reversible;
agents never write to production directly; learned changes are proposed, evaluated, shadowed and
promoted by a human. See `CLAUDE.md`.
