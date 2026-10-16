# Suite: policy-regression

> Phase 0: structure only, with no cases. First populated in **Phase 8**
> (policy proposals and offline evals) and extended in **Phase 9** (decision
> classes, BLOCK / REQUIRE_APPROVAL / AUTO_APPROVE engine). Not in TRD §19: it
> comes from the eval-learning-engineer role and serves PRD §10 `OFFLINE_TEST`.

**Exercises:** `POLICY_GATE` and the versioned ranking/eligibility policy
(TRD §12, §20).

**A passing case:** a candidate policy, replayed over historical decisions,
scores at or above the baseline policy, fails **no** protected-content or
safety case, and does not shift any decision class toward less oversight
without that being explicitly flagged.

**Example cases (prose):**
- A reweighted ranking policy should not lower agreement with past
  human-approved destinations.
- A candidate policy must still block every historical protected-page case.
- A historical change that was later rolled back should not be rated more
  favourably under the candidate policy.

**Failure modes guarded:** silent quality regression; safety regression;
overfitting to recent feedback; treating a passing result as authority to
promote. Passing only qualifies a policy for `SHADOW`; promotion stays ASK.

## Phase 8 status: populated (12 cases)

Dataset: [`evals/datasets/policy-regression/core.v1.jsonl`](../../datasets/policy-regression/core.v1.jsonl).
Gate: `min_pass_rate` 0.90, `max_regression_vs_baseline` 0.02.

Replays the same ranking path under a **candidate** weight vector (`extra.candidate_weights`,
merged onto the committed policy and re-validated by `RankingPolicy`) against recorded
human-preferred destination orders. `check: safety` asserts what no reweighting may change;
`check: candidate_weights_refused` asserts the frozen-path boundary a candidate must not cross.

Covered: the baseline agreeing with the human choice; a taxonomy-weighted candidate keeping the
first choice while reordering the tail (visible in Kendall tau, not in precision@1); a negative
weight and a weight set not summing to one both refused by the contract; a candidate still
unable to reach a protected page, an ineligible page, or a pair inside the rejection cooldown;
a rolled-back-then-unpublished destination not resurrected; an invented ranking component
refused as not allowlisted; and a candidate smuggling a placement change refused as a frozen
path.

**Two cases pin a known regression on purpose** — a geography-blind candidate (which stops
distinguishing the India report from the Asia Pacific one and loses the first choice on a tie)
and a commercial-plus-under-linked-only candidate (which discards topical fit entirely). They
assert that the regression *still happens*, so it cannot quietly disappear; they are not
assertions that those candidates are acceptable. Run-level precision@1 is 0.667 and Kendall tau
0.417 on this suite as a direct result, which is the number a human would read before promoting
anything.

Passing still only qualifies a candidate for SHADOW. Promotion remains ASK, and in Phase 8 it
is refused in code regardless.
