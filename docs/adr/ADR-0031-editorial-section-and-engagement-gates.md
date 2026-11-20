# ADR-0031 — Editorial section and engagement gates

- **Status:** Accepted by the owner, 2026-11-20
- **Scope:** recommendation placement, contextual-copy review and regional sibling surfaces

## Decision

1. `Market Overview` is a hard inline-placement exclusion. It describes the current report's
   market and must not be edited to introduce another market.
2. `Related Reports` is a hard inline-placement exclusion. The regional-sibling module builder is
   disabled because the website already supplies that surface.
3. Regional reports with the exact same market remain valid candidate relationships, but they must
   earn a natural placement in Growth Drivers, Market Trajectory, explanatory Segmentation, or
   Regional Analysis. Otherwise the correct result is no link.
4. Engine-created anchors use the destination's complete market phrase. Partial title fragments
   are not enough to predict what the reader will receive.
5. Relevance is necessary but insufficient. A proposal must explain the additional information a
   reader gains and must still read naturally without the hyperlink.

## Consequences

- Placement code deterministically skips blocks governed by Market Overview or Related Reports
  headings before score can influence selection.
- Existing module rows are cleared on the next recommendation or module-only run.
- Shadow recommendations retain excluded proposals as learning evidence, but they are marked
  ineligible and are hidden from the default review queue.
- Existing live links are not automatically rolled back. A production rollback remains a separate,
  explicit and auditable action.

