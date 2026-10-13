# Suite: relationship-detection

> Phase 0: structure only, with no cases. First populated in **Phase 2** (taxonomy,
> entities, graph relationships); formal dataset in Phase 8. TRD §19.

**Exercises:** `UNDERSTAND` (Understanding agent, TRD §10) and graph-edge
creation.

**A passing case:** the extracted classification (market, industry,
geography, page type and so on, per PRD §7.2) matches the expected labels, and
each detected edge uses the correct PRD §7.3 type (`SAME_MARKET`,
`PARENT_MARKET`, `SUBMARKET`, `REGIONAL_PARENT`, `SUPPLY_CHAIN_UPSTREAM`, …)
with the correct direction.

**Example cases (prose):**
- Two reports on the same market in different countries should yield
  `SAME_MARKET`, not `SAME_GEOGRAPHY`.
- A component-supplier market and the finished-goods market it feeds should
  yield a supply-chain edge with the correct upstream/downstream direction.
- Two pages that share only a generic word, such as "services", should yield
  no edge.

**Failure modes guarded:** inverted edge direction; spurious edges from
lexical overlap; misclassified geography or region; an output that is not a
valid typed classification (it must fail at the Pydantic boundary, TRD §6).
