# Interlinking engine

Reference for engineers integrating the engine with a CMS. Every statement below was checked against the module cited. Where something could not be verified it says "not verified". Governing decisions: ADR-0011 (recommendations), ADR-0017 (vectors), ADR-0018 and ADR-0019 (autonomy, critic). Where an ADR and the code differ, this document follows the code.

## Pipeline at a glance

```
inventory -> intelligence -> (graph metrics) -> candidate generators -> deterministic filters
  -> relevance score -> anchor + placement -> final score + caps -> LinkPlan (proposed)
  -> critic (agents) -> autonomy engine -> publish / verify / rollback
```

Commands, in order: `make ingest-file` or `make crawl`, `make analyze`, `make graph-metrics`, `make qdrant-index` (optional), `make recommend`, `make agents`, `make autonomy` or `make autonomy-run AS_USER=<name>`.

## 1. Inventory and intelligence

Inventory (`services/ken_services/inventory/`) stores pages, immutable page versions, content blocks and the links found in each version. Intelligence (`services/ken_services/intelligence/`) produces per-version analysis (market, industry, intent, keywords, commercial importance), embeddings and taxonomy relationships. The recommendation engine reads only the current version of each page, and only analysis that matches the current version and analysis version. Detail of the analysis and embedding recipes is not covered here and was not re-verified for this document.

## 2. Graph metrics

`make graph-metrics` runs `services/ken_services/graph/compute.py` as a recorded `graph_metrics` job. It reads live pages, edges and cluster keys, computes everything in memory, and replaces all rows of the current `metrics_version` in `page_graph_metrics` in one transaction. Rows of other versions are never touched.

- **Graph definition** (`services/ken_core/db/repositories/graph.py`): nodes are pages that are not gone. An edge is an internal body link of a node's current version that resolves through `known_urls` to another live page. URLs last seen as not-found or gone are excluded. Edges are distinct directed page pairs; self-links are dropped (`graph/algorithms.py`).
- **in/out degree**: count of distinct linking / linked pages.
- **Internal PageRank**: damped power iteration, dangling mass spread uniformly. **Hub and authority**: Kleinberg HITS, each vector scaled to a maximum of 1. Parameters come from `policies/graph/graph.v1.yaml` (damping 0.85, tolerance 1e-10, 200 iterations).
- **Crawl depth**: shortest directed click path from pages whose URL path matches `entry_path_patterns` (default `^/$`). No entry page found means `crawl_depth` is NULL everywhere; it is never guessed.
- **Components**: weakly connected, ordinal 0 largest. **Bridge**: articulation point of the undirected graph. **Clusters**: pages sharing their most specific market, else industry, from the newest analysis; ordinal 0 is the largest and ordinals are stable only within one `metrics_version`.
- **Orphan**: zero distinct inbound pages. **Nearly orphan**: at most `nearly_orphan_max_inbound` (policy value 1) inbound pages.
- Determinism: sorted node order and id tie-breaks, no randomness. `metrics_version` is `graph-alg<N>-pol<M>` (`graph/policy.py`).

### The home page is flagged `is_orphan`

The database enforces `is_orphan = (in_degree = 0)` and `NOT is_orphan OR is_nearly_orphan` (CHECKs `ck_page_graph_metrics_orphan_iff_no_inbound` and `ck_page_graph_metrics_orphan_implies_nearly`, migration `0010_qdrant_and_autonomy.py`). The crawl entry page is normally reached from outside the site and has no inbound body link, so it is stored `is_orphan = true` although it is not a page to rescue. Consumers must use `is_entry_page` (`crawl_depth == 0`, `contracts/python/ken_contracts/graph.py`) and `true_orphans` (orphans that are not the entry page) from `services/ken_services/graph/reader.py`. A true orphan is `is_orphan` with `crawl_depth` NULL or above 0. The raw `orphans` counter includes the entry page by design.

## 3. Candidate generators

`services/ken_services/recommendations/generators.py` defines six independent generators, unioned by `retrieve()` in `channels.py`. The default policy (`policies/recommendation/recommendation.v1.yaml`) enables four; `analytics_k` and `orphan_recovery_k` default to 0 in `contracts/python/ken_contracts/recommendations.py`, so the other two are inert until an operator sets them.

| Generator | Nominates | Separate signal(s) |
|---|---|---|
| semantic | nearest current embeddings via `VectorStore.search`; stale hits re-checked and dropped against PostgreSQL | `semantic_similarity` |
| lexical | PostgreSQL full text, `ts_rank_cd` of the source's keywords over current page versions | `lexical_similarity` |
| topic (channel name `taxonomy`) | stored `page_relationships` from the source | `topic_relevance` |
| graph | low in-degree pages in the source's industry; falls back to SQL inbound counts if no metrics exist | `graph_in_degree`, `graph_pagerank`, `graph_authority` |
| analytics | demand without supply from stored GSC/GA4 rows | `gsc_opportunity`, `ga4_engagement` |
| orphan recovery | orphan or nearly-orphan pages in the source's industry or cluster; the entry page is never a target | `orphan_recovery_value` |

If no embedding provider is reachable the semantic generator returns nothing. Analytics may surface and may block but never alone justify: a candidate nominated only by analytics is kept only if cosine reaches the semantic floor or a stored taxonomy relationship supports it (`AnalyticsSupportGate`, which can only return blocks). The union is capped at `max_candidates_per_source` (80).

When `KEN_VECTOR_STORE=memory` (the default) the pipeline builds a process-local store and runs one incremental index pass per run before generating (`recommendations/pipeline.py`). See `docs/QDRANT_ARCHITECTURE.md`.

## 4. Deterministic filters

`recommendations/filters.py`, `pair_rejection`: first rejection wins, in this order: `self_link`, `source_protected`, `target_protected`, `experiment_locked`, `source_ineligible`, `target_ineligible`, `already_linked`, `duplicate_recommendation`, `previously_rejected`, `target_excluded` (the home page via `excluded_target_paths`), `page_type_incompatible` (page-type table or zero intent compatibility). Every candidate, kept or rejected, is recorded with its code. No model is involved.

## 5. Ranking

`recommendations/scoring.py`: `relevance = sum(weight x component)` over eight components in [0, 1], using `policies/ranking/ranking_weights.v1.yaml` (semantic 0.30, taxonomy 0.20, intent 0.15, graph 0.15, geography 0.08, commercial 0.05, underlinked 0.04, historical 0.03). Weights are versioned data; a plan records the ranking-policy version.

- The `graph` component is complementarity, `1 / (1 + links from the source into the target's market)`. It is not PageRank. PageRank, hub, authority, degree, lexical similarity and the analytics signals are recorded per candidate under their own names and are not weighted.
- `historical` is 0 until calibrated outcome evidence exists.
- Candidates below `selection.min_relevance` (0.30) are rejected before placement.
- After placement: `total = relevance x placement_quality`, and plans below `min_score` (0.35) are rejected. The breakdown, both policy versions and per-channel attribution are stored (`explain_ranking`).
- `confidence` is the raw `total` and `calibration_version` is `NULL` at proposal time (`pipeline.py`). The autonomy engine is the component that looks for a calibration.

## 6. Anchor selection and placement

`recommendations/placement.py`. Anchors are never invented. Candidate phrases come from the target's market label and aliases, its other confident topics, its title core, and keyword bigrams. Each must pass word-count, length and forbidden-anchor rules and must not be a geography label. A phrase must occur verbatim, whole-word and case-insensitively, in an allowed block (`p`, `li`) that holds no link yet, has no excluded ancestor (tables, quotes, headings, code, existing links and others in the policy) and is not a fragment of a longer phrase. The anchor text is copied verbatim from the block. `placement_quality` is 1.0 for topic, 0.9 for title, 0.75 for keyword anchors, minus 0.1 in the opening block. After placement, caps apply in order: `link_density_exceeded`, `per_source_limit` (3 open plans per source), `diversity_limit` (5 open plans per target).

### Stale-source protection

A plan stores the source version, a `context_hash` of the block, and the span offsets. `recommendations/freshness.py` classifies a plan on a newer source version as fresh, rebasable (identical block text and kind, anchor still at the span, still placement-safe) or stale. Stale plans are moved to `expired` on the next run. Each run also re-validates open plans and expires those whose source or target is now durably ineligible. Deployment repeats the check against the CMS's current HTML (`services/ken_services/deployment/mutation.py`). The autonomy engine reblocks with `STALE_SOURCE_VERSION` and `INVALID_ANCHOR`.

## 7. Critic

`make agents` runs the LangGraph review in `agents/ken_agents/`. It reviews up to `max_plans_per_run` (3) proposed plans per source. Outcomes (`agents/ken_agents/critic.py`, ADR-0019): `accept` becomes ACCEPT; `reject` becomes REJECT; `uncertain` and anything else becomes NO_ACTION. Only an accept is handed on (`proposed -> critiqued -> policy_checked`); other reviews are recorded and the plan stays `proposed`.

- Deterministic guards run after the model and can only move an outcome toward REJECT or NO_ACTION: generic anchor, duplicate destination, same anchor for another page, overlapping span.
- A model problem (unavailable, timeout, schema violation) yields `uncertain`, never accept. Only `critic.use_model: false` applies the threshold rule (accept at raw confidence >= 0.5).
- `policies/agents/agents.v1.yaml` sets `critic.use_model: true`. With no reachable model, every plan is therefore NO_ACTION and nothing reaches the autonomy engine until a model is available or the policy is changed in a new version.
- The agent package may not import `ken_services.autonomy`; the critic is a signal and cannot clear a deterministic block.

## 8. Hand-off

Plans at `policy_checked` (and earlier statuses in `DECIDABLE_STATUSES`) are decided by the autonomy engine. See `docs/AUTONOMY_ENGINE.md`.

## What is deliberately not done

- No Neo4j. The graph is computed in application memory over PostgreSQL rows.
- No mandatory graph neural network. `services/ken_services/graph/experiments.py` is an offline seam; nothing in the recommendation pipeline imports it, and a candidate must show benchmark evidence (at least 100 held-out cases and a stated margin) to replace the baseline.
- No signal collapsed into one opaque score. The weighted components, the separate signals and the placement factor are stored side by side.
- No model writes plans. The deterministic engine is the only producer of `proposed` plans.

## Provenance

Five public internal-linking repositories were studied as research references; see `docs/research/INTERLINKING_REFERENCE_REPOS.md`. Their influence is conceptual only: no code, data, prompts or configuration were copied, and none is a dependency.
