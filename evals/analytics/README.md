# Analytics evaluation fixtures (Phase 7)

`attribution/golden_scenario.json` is the golden attribution scenario required by
ADR-0015 §9. It is **data, not code**: one scripted history over one small site, with the
exact expectation for every case Phase 7 has to get right.

| Case | What it pins down |
|---|---|
| `applied` | the full chain (run → versions → agent → human → deployment), and that it measures once `D0 + 28` is covered by real data |
| `rolled_back_mid_window` | a whole-deployment rollback closes the live window at the `rolled_back` transition |
| `per_plan_remove` | a per-plan REMOVE rollback closes it the same way — the `deployment_items` stage is never consulted |
| `superseded` | a replaced plan was never on a page (`deployed_at = null`) |
| `never_deployed` | an unapproved plan was never on a page |
| `overlapping_on_same_source` | a second plan on the same source raises `overlapping_changes` on both |
| `inconclusive_verification` | a plan stuck in `deployed` never measures |
| `content_changed` (on `applied`'s target) | a new `page_versions` row inside the windows raises `content_changed` |

The fixture is consumed by `tests/integration/test_analytics_attribution.py`, which builds
the history with the REAL transition service, imports page and link metrics through the REAL
importer, and asserts the attribution, the live windows, the confounder flags and the
`verified → measured` transitions exactly as recorded here. The same test asserts that
synthetic data never measures.

Changing an expectation means changing the fixture deliberately, in a commit that says why.
