# Suite: reflection-grounding

> **Phase 8, hard fail, 13 cases.** New in ADR-0016 §7 (kept by §15.3). Dataset:
> [`evals/datasets/reflection-grounding/core.v1.jsonl`](../../datasets/reflection-grounding/core.v1.jsonl).

**This is the anti-fabrication gate.** Every number in a proposal's evidence must re-derive
from the data snapshot it claims to come from. A proposal whose evidence does not re-derive is
a hard fail, and a hard-fail suite cannot be waived by any policy value.

**Exercises:** the shape of `ken_contracts.learning.DecisionClassObservation` and a
deterministic re-derivation of every count and rate from raw reviewed-plan rows. It is the
check that makes "no model-produced number appears in `evidence`" measurable rather than
merely asserted by a static test.

**A passing case:** every claimed `sample_count`, `approvals`, `edits`, `rejections`,
`rollbacks`, `positive_outcomes`, the five rates derived from them, and the `sufficient` flag
all equal what the snapshot's rows produce — and the claim neither invents a decision class
nor silently drops one.

**Example cases (prose):**
- Counts and rates that re-derive exactly are accepted (the control).
- An approval count one higher than the rows support fails, even when the claim is internally
  consistent — its counts and rates agree with each other, so only re-deriving catches it.
- 6 of 8 is 0.75; a rate rounded to 0.8 in the proposal's favour fails.
- Claiming no edits when two reviewers edited fails: an anchor edit is an approval *with a
  change*, and losing it would erase the signal reflection actually learns from.
- Claiming no rollbacks when one deployment was rolled back fails. A rollback is the strongest
  negative editorial signal there is.
- An inflated `positive_outcome_rate` fails too, although that signal may only ever *block* a
  gate — which means fabricating it upward could only be an attempt to hide a block.
- Claiming `sufficient: true` with 8 samples against a floor of 30 fails. This is the
  mechanism that keeps Phase 8 `inconclusive`; if a proposal could assert its own sufficiency,
  `min_sample_count` would be decorative.
- Evidence for a decision class with no rows at all fails (`absent_class`), and evidence that
  drops a class the snapshot holds fails (`omitted_class`) — selective reporting is as
  misleading as a wrong number.
- A total sample count that is not the sum over its classes fails: that total is the first
  number a human reads on the promotion screen.
- An observation whose counts cannot coexist (7 approvals + 2 rejections in 8 samples) is
  refused at the Pydantic boundary before any re-derivation.

**Failure modes guarded:** a model asked for "the numbers" producing plausible ones; a
rounding that always favours the proposal; a fabricated decision class generalised from classes
that do exist; selective omission of the class where humans disagreed; a proposal asserting its
own statistical sufficiency; arithmetic that does not add up on the screen a human approves
from.

**Not covered:** the snapshots are hand-counted synthetic rows, not real `review_actions`. This
suite proves the *re-derivation* is sound; it says nothing about Ken Research's editorial data,
which does not exist yet.
