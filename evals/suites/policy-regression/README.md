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
