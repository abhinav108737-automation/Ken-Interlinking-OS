# Suite: autonomy-classification

> **Phase 8 completion, hard fail, 36 cases.** Added by the final-completion directive and
> ADR-0018. Dataset: [`evals/datasets/autonomy-classification/core.v1.jsonl`](../../datasets/autonomy-classification/core.v1.jsonl).
> Hard-fail and unwaivable (`HARD_FAIL_SUITES`), gated at `min_pass_rate` 1.0.

**Why this is a hard gate.** The autonomous path has no human to catch a wrong decision, so the
classifier is the last line of defence. The central safety property is that **a model may lower
and never raise**: a critic that answered ACCEPT must not turn a deterministically blocked plan
into a publish.

**Exercises (real code):** `evaluate_blocks` (the closed 15-member `BlockCode` set),
`critic_outcome_of`, `tier_from_signals` / `raise_to`, `DatabaseAutonomyEngine._assemble` (which
calls `decision_for`, applies the mode's confidence and calibration floors, calls `profile_for`
and builds the contract-validated `AutonomyEvaluation`), and both `target_status_for` and the
engine's own `_target_status`.

**A passing case:** the decision string `"[<fired codes>] mode:decision[/status[/profile]],…"`
equals the expectation for every mode the case names, and no `must_not` token appears.

**Cases (prose):**
- **Every `BlockCode`, one case each (15):** a plan whose only defect is that code yields BLOCK in
  shadow, guarded and autonomous mode. A unit test asserts all 15 values are covered, so adding a
  code to the enum fails the suite until a case exists for it.
- **The central property (3 cases):** a *recorded* critic ACCEPT answer, at a calibrated 0.99
  confidence, on a protected page, on a plan with no rollback metadata and on a 404 destination —
  still BLOCK in every mode.
- Codes accumulate rather than short-circuit (`stale_source_version` + `invalid_anchor`).
- **Shadow** records the counterfactual (`would=…`) and moves the plan nowhere.
- **Guarded** publishes only low-risk, high-confidence, *calibrated* plans and **defers**
  (`awaiting_approval`, not `blocked`) a merely-too-risky one. Deferral cases: medium risk, high
  risk, confidence under 0.60.
- An **uncalibrated** class gets the conservative decision: guarded defers it and autonomous
  publishes it only with the STRICT profile — never plain `auto_publish`.
- `AUTO_PUBLISH_WITH_EXTRA_VERIFICATION` differs only in the verification profile; a strict
  installation default is never downgraded by a plain publish.
- A critic REJECT blocks with no code fired; a critic UNCERTAIN raises the tier; a critic
  `no_action` is an earlier outcome than a decision and never becomes a publish.
- **Behavioural signals only raise risk:** a measured rollback makes a plan high-risk, no signal
  yields the lowest tier, and a lower floor never lowers a tier. A unit test checks monotonicity
  over all 256 signal combinations.
- **Controls:** a clean plan publishes under guarded and autonomous mode; a standard-profile plain
  publish; the lowest tier is reachable.

**One case PINS A DIVERGENCE on purpose.** The frozen helper `target_status_for` sends a guarded
deferral to `awaiting_approval` (ADR-0018 s3). The engine's `_target_status` instead records it as
`blocked`, because migration 0010's `ken_autonomy_decision_guard()` maps every `block` decision to
`blocked` and raises otherwise. The suite asserts *both* (the deferral cases use the frozen
helper; `au-the-engine-records-a-guarded-deferral-as-blocked` pins the engine). They disagree, and
the case is expected to fail the day they are reconciled.

**Not covered here (database behaviour — see `tests/integration/`):** `load_plan_facts`, the SQL
snapshot that produces `PlanFacts` (each case supplies the facts instead), and the recording of the
`autonomy_decisions` row with its deferred guard. The `_gather` glue between the critic, the risk
tier and `extra_verification` (three lines that need a session) is mirrored in the scorer and
marked there; the decision logic after it is the production code.
