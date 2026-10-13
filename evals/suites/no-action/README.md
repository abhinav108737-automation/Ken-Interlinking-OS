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
