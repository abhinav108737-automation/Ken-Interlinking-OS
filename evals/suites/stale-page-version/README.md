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
