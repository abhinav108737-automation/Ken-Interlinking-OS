# workflows/ — reserved (arrives Phase 4)

Will hold the LangGraph supervisor graph definitions (TRD.md §9):

```
START → LOAD_PAGE → TRIAGE → UNDERSTAND → RETRIEVE → FILTER → RANK → PLACE → CRITIQUE
      → POLICY_GATE → HUMAN_APPROVAL | AUTO_APPROVE → VALIDATE → PUBLISH → VERIFY → MEASURE → END
```

**Nothing here in Phase 0.** LangGraph is deliberately not yet a dependency (ADR-0005): adding it before
there is a graph to run buys an unused dependency tree.

Rules the Phase 4 graph must honour:

- Any node may return a **typed recoverable error** (TRD §9).
- **Never leave a live process waiting for a human.** Persist the task as `AWAITING_APPROVAL` and resume
  from an approval event (TRD §16).
- `AUTO_APPROVE` stays **disabled globally in v1** (TRD §20); the branch exists only as a policy outcome.
- Probabilistic reasoning **terminates at a typed deterministic boundary** before any irreversible action
  (TRD §24).
