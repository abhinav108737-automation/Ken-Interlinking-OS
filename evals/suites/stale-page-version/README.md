# Suite: stale-page-version

> Phase 0: structure only, with no cases. First populated in **Phase 6**
> (publishing, optimistic locking); formal dataset in Phase 8. TRD §19.

**Exercises:** `VALIDATE` → `PUBLISH`, run against the mock CMS. Mutation
requests carry `expected_version`, and a mismatch returns 409 or raises
`CMSVersionConflictError`, which **forces regeneration** (TRD §13,
docs/CONTRACTS.md rule 5).

**A passing case:** when the page changes between planning and publishing,
the old plan is never published. The task regenerates against the new
version, and the conflict is audited.

**Example cases (prose):**
- The source body is edited after approval. Publishing must fail on version
  and the plan must be regenerated, not retried blindly.
- The anchor's paragraph is removed in the new version. The regenerated plan
  must not reuse the old offsets.
- The same `operation_id` is replayed after success. The outcome must be
  idempotent, with no duplicate link.

**Failure modes guarded:** last-write-wins overwrites; retrying with a
refreshed `expected_version` but a stale plan; stale offsets landing on the
wrong text; duplicate mutations.

## Phase 8 status: populated (16 cases) — HARD FAIL

Dataset: [`evals/datasets/stale-page-version/core.v1.jsonl`](../../datasets/stale-page-version/core.v1.jsonl).
Gate: `min_pass_rate` **1.0**, `max_regression_vs_baseline` 0.0, in `HARD_FAIL_SUITES` and
unwaivable.

Three layers:
- `check: freshness` — `recommendations.freshness.check_plan_freshness`. **The reasons are part
  of the decision**, not a note: `freshness:stale:version_changed+block_missing` versus
  `…+context_changed+anchor_moved` is the difference between two regeneration paths, so the
  case asserts *why* a plan is stale.
- `check: mutation` — `deployment.mutation.HtmlMutator.insert_links` against the **new** body
  with the plan's old locator.
- `check: cms` — the mock CMS adapter's `expected_version` precondition and its idempotency
  ledger.

Covered: unchanged version is fresh; an unrelated edit elsewhere is *rebasable*, not stale; a
reordered-but-identical paragraph is still rebasable (block ids are positional, so treating the
id as identity would expire a plan whose anchor never changed); a deleted paragraph is stale
with `block_missing`; an edited paragraph with `context_changed`; a removed anchor phrase adding
`anchor_moved`; identical text moved into a table cell and identical text whose anchor an editor
has already linked, both stale because placement safety is re-checked and not inferred from the
text hash alone. On the mutation side: a fresh plan splices once; a missing block, an edited
context, an already-linked anchor and a table cell each refuse with a specific issue code and
leave the HTML unchanged. On the CMS side: a version mismatch is a conflict, a matching version
applies, and a replayed `operation_id` returns the recorded deployment rather than a second
link. Three of those are controls, so "refuse everything" cannot pass this suite.
