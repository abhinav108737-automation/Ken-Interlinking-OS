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

## Phase 8 status: populated (13 cases)

Dataset: [`evals/datasets/relationship-detection/core.v1.jsonl`](../../datasets/relationship-detection/core.v1.jsonl).
Gate: `min_pass_rate` 0.75, `max_regression_vs_baseline` 0.05.

Drives `relationships.taxonomy_relationships` over the committed taxonomy. The decision is the
**primary** edge — strongest by weight, ties broken by `RelationshipType` declaration order,
which is exactly the rule `scoring.score_pair` uses, so the suite measures the relationship a
`LinkPlan` would actually record. `must_not` is evaluated against *every* detected edge, so a
case can assert that a spurious edge type does not exist at all.

Covered: every PRD §7.3 type the taxonomy can produce — `same_market` across two countries,
`parent_market` and `submarket` as a direction pair (each forbidding the other, which is what
catches an inverted edge), both supply-chain directions, `complementary_market`,
`related_market` between siblings, `regional_parent`, `same_geography` alone,
`industry_adjacency` alone — plus the no-edge case on pages sharing only the word "market", a
page with no geography at all, and a page against itself.
