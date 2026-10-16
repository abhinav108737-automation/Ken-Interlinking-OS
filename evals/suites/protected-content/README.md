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

## Phase 8 status: populated (14 cases) — HARD FAIL

Dataset: [`evals/datasets/protected-content/core.v1.jsonl`](../../datasets/protected-content/core.v1.jsonl).
Gate: `min_pass_rate` **1.0**, `max_regression_vs_baseline` 0.0, and the suite is in
`HARD_FAIL_SUITES`, which **no policy value can waive** — the contract validator refuses a
`hard_fail_suites` list that drops it, and the runner unions the policy's list with the
contract's own.

Protection is **computed, not declared**: a fixture's path is run through the real
`intelligence.protection.match_protections` against the committed taxonomy, so `/privacy-policy`
is protected because the shipped policy says so.

Three layers, all driven for real:
- `check: pair` — `filters.pair_rejection`, where protection is checked third, before
  eligibility and before any score exists;
- `check: revalidate` — `validity.plan_invalidity`, the re-check under the plan's row lock
  before an approval or an edit;
- `check: cms` — the mock CMS adapter's own refusal, which is the last line of defence.

Covered: a protected destination and a protected source; a locale-prefixed `.html` legal path;
an operator's manual protection beating the strongest possible relevance; protection outranking
both thin content and `already_linked` in the rejection code (so the audit record names the
rule that actually fired); an experiment-locked pair; re-validation catching a newly protected
source, a newly protected target, a newly unpublished target and a newly existing link; the CMS
refusing a mutation on a protected page with nothing applied; **and two controls** — an
eligible pair is allowed, and an unchanged plan stays valid. A filter that rejected everything
would otherwise score 100% on a zero-tolerance suite.
