# Suite: no-action

> Phase 0: structure only, with no cases. First populated in **Phase 4**
> (Triage and Planner nodes). It becomes a formal dataset in Phase 8. TRD §19
> and §22 ("recorded fixtures for … no-action").

**Exercises:** `TRIAGE` (returns `NO_ACTION`, `REINDEX`, `OPTIMIZE`,
`REPAIR_LINK` or `REVIEW_PERFORMANCE`, TRD §10) and the Planner's ability to
return zero plans.

**A passing case:** when no change is warranted, the system ends without
producing a `LinkPlan`, emits no mutation and records why.

**Example cases (prose):**
- A page that is already well linked to all of its strong candidates should
  yield `NO_ACTION`.
- A trivial edit, such as a typo fix, on a stable page should not trigger
  re-optimization.
- A source whose only candidates are weak should produce zero plans rather
  than the "least bad" link.

**Failure modes guarded:** action bias, i.e. always proposing something;
overlinking; churn on unchanged content; wasting reviewer attention on
low-value proposals.

## Phase 8 status: populated (11 cases)

Dataset: [`evals/datasets/no-action/core.v1.jsonl`](../../datasets/no-action/core.v1.jsonl).
Gate: `min_pass_rate` 0.90, `max_regression_vs_baseline` 0.02.

Two kinds of case. `check: triage` drives **the real `ken_agents.nodes.triage` node** over an
in-memory `AgentBackend`; the decision is `triage:<decision>`. `check: selection` drives the
pair chain and `score_pair`, then the `min_relevance` floor and the `min_score` floor, and the
decision is `plans:<n>`.

The selection check applies the final-score floor at the best possible placement quality
(1.0), so it is an *upper bound* on what the engine would keep — which makes a case asserting
`plans:0` a strictly stronger claim than the engine's own behaviour.

Covered: a gone source; missing analysis and missing eligibility both yielding `reindex` rather
than `no_action` (an operational gap must be reported, not hidden); a protected source; a hub;
a source below the word-count floor; no unreviewed plan; **and the two controls** — an
unreviewed plan yielding `optimize`, and two strong candidates yielding two plans. Without
those controls a triage that always answered `no_action` would score 100% here and the suite
would measure nothing.
