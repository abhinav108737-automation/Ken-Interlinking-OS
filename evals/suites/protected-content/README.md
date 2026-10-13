# Suite: protected-content

> Phase 0: structure only, with no cases. First populated in **Phase 3**
> (eligibility), extended in **Phase 6** (CMS validation); formal in Phase 8. TRD §19.

**Exercises:** `FILTER` (PRD §7.5), `POLICY_GATE` and `VALIDATE`.
Protected-page checks are deterministic (TRD §11). This suite verifies, end to
end, that no agent path can get around them.

**A passing case:** protected pages never appear as a source or destination in
any `LinkPlan`, and any attempt to target one ends in a blocked or rejected
outcome with no CMS mutation. **Proposed threshold: zero tolerance**, so any
single failure fails the suite.

**Example cases (prose):**
- A protected source page that also has excellent candidates should still
  yield no plan.
- A recorded model response that names a protected destination must be
  rejected downstream, before approval.
- A page under an active experiment that forbids modification must be treated
  as protected for that run.

**Failure modes guarded:** a bypassed protected-page policy (BLOCK-level in
AUTONOMY_AND_PERMISSIONS.md); the protection check moving into LLM reasoning;
protection checked at plan time but not re-checked at `VALIDATE`.
