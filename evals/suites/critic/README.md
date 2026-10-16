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

## Phase 8 status: populated (12 cases)

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
