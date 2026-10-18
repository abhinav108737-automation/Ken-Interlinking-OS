# Suite: autonomy-classification

> **Phase 8 completion, hard fail, 45 cases.** Added by the final-completion directive and
> ADR-0018. Dataset: [`evals/datasets/autonomy-classification/core.v1.jsonl`](../../datasets/autonomy-classification/core.v1.jsonl).
> Hard-fail and unwaivable (`HARD_FAIL_SUITES`), gated at `min_pass_rate` 1.0.

**Why this is a hard gate.** The autonomous path has no human to catch a wrong decision, so the
classifier is the last line of defence. The central safety property is that **a model may lower
and never raise**: a critic that answered ACCEPT must not turn a deterministically blocked plan
into a publish.

**Exercises (real code):** `evaluate_blocks` (the closed 15-member `BlockCode` set),
`critic_outcome_of`, `tier_from_signals` / `raise_to`, `DatabaseAutonomyEngine._assemble` (which
calls `decision_for`, applies the floors from `policies/autonomy/autonomy.v1.yaml` -- guarded
0.85, autonomous 0.50 --, calls `profile_for` and builds the contract-validated
`AutonomyEvaluation`), `target_status_for`, the `AutonomyEvaluation.deferred` property, and
`blend_confidence` for the calibration arithmetic.

**A passing case:** the decision string
`"[<fired codes>] mode:decision[/status[+deferred][/profile]],…"` equals the expectation for
every mode the case names, and no `must_not` token appears. A case may carry `variants`, run
under several overrides and joined with ` | `, which is how one case puts a hard block and a
deferral side by side.

**Cases (prose):**
- **Every `BlockCode`, one case each (15):** a plan whose only defect is that code yields BLOCK in
  shadow, guarded and autonomous mode. A unit test asserts all 15 values are covered, so adding a
  code to the enum fails the suite until a case exists for it.
- **The central property (3 cases):** a *recorded* critic ACCEPT answer, at a calibrated 0.99
  confidence, on a protected page, on a plan with no rollback metadata and on a 404 destination —
  still BLOCK in every mode.
- Codes accumulate rather than short-circuit (`stale_source_version` + `invalid_anchor`).
- **Shadow** records the counterfactual (`would=…`) and moves the plan nowhere.
- **Guarded** publishes only low-risk, high-confidence (>= 0.85), *calibrated* plans and
  **defers** a merely-too-risky one. A deferral is recorded as `blocked` with an EMPTY block list
  (migration 0010's guard maps every `block` decision to `blocked`), and
  `AutonomyEvaluation.deferred` is the explicit property that marks it. Deferral cases: medium
  risk, high risk, confidence under the floor. One case puts a protected-page hard block beside a
  deferral and shows they differ **only** by `deferred`; another shows a critic rejection is a
  refusal and not a deferral; another that shadow rows are never deferrals.
- An **uncalibrated** class gets the conservative decision: guarded defers it and autonomous
  publishes it only with the STRICT profile — never plain `auto_publish`.
- `AUTO_PUBLISH_WITH_EXTRA_VERIFICATION` differs only in the verification profile; a strict
  installation default is never downgraded by a plain publish.
- A critic REJECT blocks with no code fired. A critic UNCERTAIN is `no_action`, ALWAYS --
  including a legacy row recorded as handed on -- and a `no_action` is an earlier outcome than a
  decision: it never raises a tier, never demands extra verification and never becomes a
  publish (`au-a-legacy-uncertain-critic-is-declined-and-never-decided`, dataset v2).
- **Behavioural signals only raise risk:** a measured rollback makes a plan high-risk, no signal
  yields the lowest tier, and a lower floor never lowers a tier. A unit test checks monotonicity
  over all 256 signal combinations.
- **Reachability of plain `AUTO_PUBLISH`:** an uncalibrated class is never plain `AUTO_PUBLISH`
  at raw confidence 0.5 or 0.99 (guarded defers it, autonomous adds strict verification); a
  calibrated class reaches it (the control); a calibrated but commercial (high-risk) plan still
  gets extra verification. Calibration arithmetic via the real `blend_confidence`: it never lifts
  a confidence, a 25% rollback rate caps it at 0.75, a low positive-outcome rate caps it, and a
  perfect positive-outcome rate changes nothing (first-party counts are spoofable).
- **Controls:** a clean plan publishes under guarded and autonomous mode; a standard-profile plain
  publish; the lowest tier is reachable.

**History, so nobody re-opens it.** An earlier version of this suite pinned a divergence
between the frozen `target_status_for` (which sent a guarded deferral to `awaiting_approval`) and
the engine (which recorded it as `blocked`). The autonomy workstream reconciled it in favour of
the database guard: `target_status_for` now returns `blocked` for a deferral and `deferred` is an
explicit property. The tripwire case was retired, and the deferral cases now assert `blocked`
with an empty block list and `deferred` true.

**Not covered here (database behaviour — see `tests/integration/`):** `load_plan_facts`, the SQL
snapshot that produces `PlanFacts` (each case supplies the facts instead); the calibration
LOOKUP -- that a class is calibrated only by a passing, fresh evaluation carrying a row for that
exact class with at least 30 samples and a recorded calibrated confidence
(`tests/integration/test_autonomy_reachability.py`), of which this suite measures only the
arithmetic once such a row exists; and the recording of the `autonomy_decisions` row with its
deferred guard. The `_gather` glue between the critic, the risk
tier and `extra_verification` (three lines that need a session) is mirrored in the scorer and
marked there; the decision logic after it is the production code.
