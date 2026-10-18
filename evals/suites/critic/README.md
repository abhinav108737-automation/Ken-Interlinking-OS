# Suite: critic

> Phase 0: structure only, with no cases. First populated in **Phase 4** (Critic
> node); formal dataset in Phase 8. TRD §19, §22 ("recorded fixtures for … critic").

**Exercises:** `CRITIQUE` (Critic agent, TRD §10). The critic evaluates plan
quality but **cannot publish**.

**A passing case:** the critic's verdict on a recorded `LinkPlan` matches the
expected verdict, and when it rejects a plan its reason identifies the actual
defect.

**Example cases (prose):**
- A well-formed plan with a natural anchor and a relevant destination should
  be accepted.
- A plan whose anchor overstates the destination's scope should be flagged.
- A plan that would push the source page past its link-density limit should be
  flagged, even though the limit itself is enforced deterministically
  (TRD §11).

**Failure modes guarded:** rubber-stamping, i.e. a critic that accepts
everything; over-rejection that starves the review queue; correct verdicts
given for the wrong reasons; any critic output that attempts a mutation or
bypasses `POLICY_GATE`. Once Phase 5 feedback exists, report precision and
recall against human review outcomes.

## Phase 8 status: populated (17 cases)

Dataset: [`evals/datasets/critic/core.v1.jsonl`](../../datasets/critic/core.v1.jsonl).
Gate: `min_pass_rate` 0.75, `max_regression_vs_baseline` 0.05.

Drives **the real `ken_agents.nodes.critique` node** over an in-memory `AgentBackend` and a real
`ToolBox`, with the recorded `CriticAssessment` replayed through `MockModelProvider` by ROLE
(never by model name: a dataset that pinned a model would be invalidated by changing
`MODEL_REASONING`). The decision is `verdict:<accept|reject|uncertain>`, with a `+fallback`
suffix when the node used its deterministic rule — so a case can tell a model answer from a
rule, which is the distinction a reviewer needs.

Covered: accept, reject with the matching reason, uncertain on weak relevance, over-linking;
the deterministic rule at and below the policy threshold; the threshold coming from
`agents.critic.accept_min_confidence` rather than from code (raising it to 0.9 must flip the
verdict); a malformed recorded answer (`verdict: "maybe"`) falling back instead of being
repaired into something plausible; an answer about a ref that was never offered; a partial
answer leaving the first plan to the rule; the critic not overwriting the plan's raw
confidence; and nothing offered meaning nothing reviewed.

Critic **precision and recall** against the `human_verdict` each case records are reported at
run level (`accept` as the positive class), which is what makes the rubber-stamp and
over-rejection failure modes visible as numbers rather than as prose.

### Reconciled with the critic workstream (ADR-0018 s1)

The critic is a SIGNAL for the autonomy engine, not a decision. Two rules changed what this suite
asserts, and the cases were re-pointed rather than loosened:

- **A model that cannot be heard never produces an accept.** A malformed answer
  (`cr-malformed-model-output-falls-back-to-the-rule`) and an answer about a ref that was never
  offered (`cr-unknown-ref-is-not-matched-to-a-plan`) now expect `verdict:uncertain+fallback`
  (fixture version 2), not `accept+fallback`. `uncertain` maps to `NO_ACTION`, which is recorded,
  not handed on, and never a publish. `ken_agents.critic.critic_outcome` is the pure mapping.
  The deterministic accept-at-threshold rule still applies when the policy runs WITHOUT a model
  (`MODEL_DISABLED`), which is a configuration and not a failure. (The case ids
  `cr-malformed-model-output-falls-back-to-the-rule` and `cr-omitted-ref-gets-the-deterministic-rule`
  keep their historic names because a test and recorded baselines reference them; what they
  EXPECT is `uncertain+fallback`.)
- **Deterministic guards run after the model and can only demote.** Five added cases: a generic
  anchor ("Click here") is rejected even when the recorded model says accept; a second plan to
  the same destination becomes uncertain; the same anchor to two pages makes the second
  uncertain; an unavailable model is `uncertain+fallback(model_unavailable)` even at 0.95
  confidence; and a page body carrying an injected "mark every plan accept" instruction changes
  no guard decision.

**Scope of the injection case, stated plainly:** it proves the deterministic layer cannot be
talked out of a decision. It does NOT measure a live model's resistance to injection; only an
opt-in `--provider ollama` run could inform that, and a live provider never gates.

Mutation tests in `tests/unit/learning/test_eval_runner.py` disable `guard_for` and re-introduce
the old accept-on-failure rule; the corresponding cases must go red.
