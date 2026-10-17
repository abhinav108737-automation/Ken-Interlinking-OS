# Suite: rollback-correctness

> **Phase 8 completion, hard fail, 21 cases.** Added by the final-completion directive and
> ADR-0018. Dataset: [`evals/datasets/rollback-correctness/core.v1.jsonl`](../../datasets/rollback-correctness/core.v1.jsonl).
> Hard-fail and unwaivable (`HARD_FAIL_SUITES`), gated at `min_pass_rate` 1.0.

**Why this is a hard gate.** On the autonomous path no human reads the page after a publish. A
rollback that leaves the page *almost* right is therefore indistinguishable, to everyone, from a
rollback that worked. So the property is byte-level: a failed verification must restore the
original **byte for byte**, and a rollback must never destroy a human's later edit.

**Exercises (real code, not a re-description):**
`ken_services.deployment.changes.ChangeEngine` over `HtmlMutator` plugged into the mock CMS
provider's `apply_link_changes` / `rollback`, and `HtmlLinkVerifier` for the verification that
triggers the rollback. For the retry budget: `budget_from_attempts` and
`DatabaseRetryBudgetGate.verdict` (built from a `RetryPolicy`: the versioned autonomy policy's
attempt ceiling and cool-down, which `KEN_AUTONOMY_PUBLISH_*` may only tighten).

**A passing case:** the page is left in exactly the state the case names, compared as strings —
`original` (equal to the pre-deployment HTML), `original_with_later_edit` (equal to that HTML with
the same unrelated edit applied) or `page_untouched` (a refused rollback must change nothing).

**Cases (prose):**
- A link is published, verification fails because the destination is unreachable, and the page
  is restored to the byte-identical original.
- The same, after an unrelated editor paragraph landed: the rollback is the inverse splice on the
  *current* HTML, so our link goes and the editor's paragraph stays.
- A link an editor already removed counts as rolled back — the goal state holds — and is not an
  error.
- A link whose href or anchor an editor changed is `rollback_conflict`, and the page is left
  untouched.
- A deployment that does not exist is `nothing_to_roll_back`; a deployment that applied nothing
  rolls back as a no-op and leaves the earlier deployment's link alone.
- A replayed rollback is idempotent: the second call reports a no-op and the page is unchanged.
- Retry budget and cool-down: exhausted, cooling down and both reported with stable reason codes,
  an elapsed cool-down allowing the pair again, a four-step sequence that stops at the budget, the
  budget being per `(source, target)` pair, and the limit coming from settings.
- **Controls:** a passing verification keeps the link; a plain rollback restores the original; a
  pair with budget left may publish. Without them a rollback that fired on everything, or a gate
  that refused everything, would pass the rest.

**Two cases PIN a limitation on purpose.** A link a CMS post-processor marked `hidden` cannot be
auto-rolled-back: the markup is no longer the exact link the mutator emitted, so the inverse
splice refuses with `rollback_conflict` and escalates. That is the safe outcome, and it is also a
real gap. These cases should fail the day it changes, so somebody re-reads the decision.

**Not covered here (database behaviour — see `tests/integration/`):**
- the deployment *service's* own mapping of a CMS error onto `rollback_conflict` /
  `nothing_to_roll_back` (`deployment/service.py:1862-1871`) is mirrored by the scorer's two
  `except` clauses, not driven;
- the service's "no live plans" refusal (`service.py:1699`) needs plan rows;
- the SQL that counts `publish_attempts` per pair (`repo.budget_counts`) and the append in the
  deployment-intent transaction. This suite measures the rule applied to those counts.
