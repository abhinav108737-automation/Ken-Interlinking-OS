# ADR-0011 — Phase 3 recommendation engine: retrieve, filter, score, place

- **Status:** Accepted (Phase 3)
- **Date:** 2026-10-14
- **Contracts:** `ken_contracts/recommendations.py`. **Schema:** migration 0004. **Policy:**
  `policies/recommendation/recommendation.v1.yaml` and `policies/ranking/ranking_weights.v1.yaml`.

## Context

Phase 3 turns Phase 2's understanding into explainable, exactly placed link proposals that are good
enough for offline review. It covers:
- PRD v1.1 FR-005 to FR-008 and §12.2–12.4;
- the PRD §7.8 `LinkPlan`;
- TRD §11–12.

No publishing happens and no LLM is involved: every step is deterministic (TRD §11). Phase 4 wraps
the engine in an agent workflow; Phase 5 adds the review actions.

## Decision

### 1. Pipeline (job kind `recommendation`, `make recommend`)

For every **eligible source** (`LinkEligibility.as_source`, computed live), the engine runs:

```
retrieve (channels) → candidates → eligibility filters → score → select (caps, diversity) → place → LinkPlan
```

Every retrieved candidate is recorded in `link_candidates`: its channels, its channel scores, and
either its total score or its first rejection code. Each run therefore has a complete, explainable
audit.

### 2. Retrieval channels

Each channel can be switched off independently (`retrieval.channels_enabled`).

| Channel | Source | Notes |
|---|---|---|
| `semantic` | top `semantic_k` by cosine over current-identity page embeddings (Phase 2) | identity filter: model, dimensions, recipe |
| `lexical` | top `lexical_k` by `ts_rank_cd(page_versions.search_vector, query)` on **current** versions | the query is the OR of the source's analysis keywords, built with `plainto_tsquery` per keyword (no user input reaches it) |
| `taxonomy` | stored `page_relationships` from the source (materialised market types) | coarse types are computed per pair during scoring |
| `graph` | top `graph_k` under-linked targets (fewest inbound links, then highest commercial importance) in the source's industry | "graph opportunity" (PRD §8) |

Candidates are unioned. Each keeps the set of channels that found it.

### 3. Eligibility filters

The filters run in this order; the first that matches is recorded.

| Code | Rule |
|---|---|
| `self_link` | target == source (also a DB CHECK) |
| `source_ineligible` / `target_ineligible` | `compute_eligibility` (live): published, not gone, not protected, not noindex, canonical, HTTP 200, thick enough, linkable page type, fresh analysis |
| `target_protected` | an active protection on the target. Reported separately for safety evals, even though eligibility already implies it. |
| `already_linked` | the source's **current** version already links to the target, via `existing_links` resolved through `known_urls` (redirects and aliases count) |
| `duplicate_recommendation` | an **open** plan for (source, target) already exists (also a partial unique index) |
| `page_type_incompatible` | the target's page type is not in `page_type_targets[source type]`, or intent compatibility is 0 |
| `link_density_exceeded` | (existing internal body links + already-selected plans + 1) / words × 100 > `max_internal_links_per_100_words` |
| `below_min_score` | the final score is below `selection.min_score` |
| `per_source_limit` / `diversity_limit` | `max_plans_per_source` reached, or the target already has `max_plans_per_target_per_run` plans in this run |
| `no_natural_anchor` | placement found no supported anchor (§5): **nothing is ever fabricated** |

### 4. Score

The score is transparent, with every component in [0, 1]:

```
relevance = Σ weight[c] × component[c]      (weights = ranking policy, Σ = 1)
final     = relevance × placement_quality    (placement_quality ∈ [0.5, 1], recorded as a penalty factor)
```

| Component | Definition |
|---|---|
| `semantic` | clamp((cosine − `KEN_SIMILARITY_MIN_SCORE`) / (1 − that)): reuses the model-calibrated threshold, 0 without an embedding |
| `taxonomy` | the maximum weight over the market-structure relationship types between source and target (`taxonomy_relationships`, live) |
| `intent` | `intent_compatibility[source intent][target intent]` |
| `graph` | source–target **complementarity**: 1 / (1 + existing links from the source into the target's market) |
| `geography` | `geography_scores`: same country 1.0, target region contains a source country 0.6, same region 0.5, target global 0.3, else 0 |
| `commercial` | target `commercial_importance` / 3 |
| `underlinked` | max(0, 1 − inbound / `underlinked_inbound_threshold`), where inbound counts current internal links to the target |
| `historical` | 0 until Phase 8 provides calibrated outcome evidence |

`confidence` is the raw `final` in Phase 3. Phase 8 calibrates it. `reason_codes` are derived from
the components and evidence (for example `same_market`, `semantically_similar`,
`underlinked_target`, `anchor_matches_target_topic`). `rationale` is rendered from those codes
deterministically and never contains free text.

### 5. Placement (deterministic, never fabricated)

Blocks are the source version's `extract_blocks(body_html)` (Phase 1). Block ids equal the
normaliser's, so locators line up with `existing_links` and `LinkLocator`.

1. **Anchor candidates for target T:**
   - the aliases and label of T's `market_id` node (`anchor_source = "target_topic"`);
   - then the aliases of T's other top topics at depth ≥ 1;
   - then T's title core, which is the title minus a trailing "Market…/Report…/Outlook…" tail, only
     if it is 2 to 8 words (`"target_title"`);
   - then two-word keyword bigrams of T (`"target_keyword"`).
2. **Search:** every eligible block (`placement.block_kinds`, default `p`/`li`) of the source, for
   case-insensitive whole-phrase occurrences. A block is skipped if:
   - its existing links already reach `max_links_per_block`;
   - it already links to T;
   - the span overlaps an already selected plan's span.
3. **Anchor rules:**
   - word count between `min_anchor_words` and `max_anchor_words`;
   - at least `min_anchor_chars` characters;
   - never one of `forbidden_anchors`;
   - the anchor text is copied **verbatim** from the block (original case).
4. **Choice:** the best (anchor source rank, phrase length descending, block salience descending,
   block order, occurrence). `placement_quality` is 1.0 for a topic alias, 0.9 for a title core and
   0.75 for a keyword bigram, minus 0.1 if the block is the opening block.
5. **Locator:**
   - `block_id`, `block_kind`, `occurrence_index` (of this phrase in the block), and character
     `start`/`end` in the block text;
   - `context_hash` = sha256 of the full block text;
   - `preview` = up to 200 characters either side.

### 6. Plans: immutable content, append-only history, freshness

- `link_plans` content is **immutable**. A trigger rejects any UPDATE that changes anything except
  `status` and `updated_at`. An edit (Phase 5) creates a new plan that `supersedes` the old one, so
  what was approved is exactly what gets deployed.
- `link_plan_transitions` is append-only. It records every status change, with actor and time.
- **At most one open plan per (source, target), and per anchor span,** enforced by partial unique
  indexes.
- **Re-runs are idempotent.** Unchanged sources produce no new plans (duplicates are filtered).
  When a source gains a new version, its open plans move to **`expired`** (with a transition) and
  fresh ones are generated against the new version.
- **`check_plan_freshness(plan, current version, current blocks)`** returns one of:
  - `fresh`: same version;
  - `rebasable`: new version, but the same block text (context hash) and the anchor still at its
    offsets;
  - `stale`: the plan must be regenerated.

  Phase 6 refuses to mutate a stale plan (PRD §13 "version awareness").
- **Events:** `link.proposed` per new plan (`LinkPlanEvent`, `subject_type = link_plan`) and
  `job.completed` per run.

### 7. Opportunities

Each run also replaces `opportunities`, computed over current state:
- `orphan`: an eligible target with no inbound internal links;
- `underlinked`: commercial importance ≥ 2 and inbound below the threshold;
- `link_capacity`: an eligible source with density headroom.

Each opportunity carries a score in [0, 1] and structured evidence.

### 8. API and UI (read-only in Phase 3)

The API is `GET /recommendations`, `/recommendations/{id}`, `/recommendations/candidates`,
`/recommendations/opportunities` and `/recommendations/summary`. The UI is a recommendation list,
a detail view (highlighted placement preview, score breakdown, reasons, history) and an
opportunities dashboard. Approve, reject and edit come in Phase 5, behind authentication.

## Consequences

- Every component can be ablated or re-weighted through policy versions. Learned ranking (Phase 8)
  can replace the weighted sum only after offline evaluation.
- Placement depends on phrase matching against taxonomy aliases. The relevance threshold and the
  intent and page-type rules prevent "inserted purely because a phrase matches" (PRD §12.4). Real
  quality depends on Ken's taxonomy.
- A plan never targets a stale view. Expiry and the freshness check guard both generation and
  deployment.

## Alternatives considered

- **An LLM-chosen anchor or placement.** It is non-deterministic and risks fabricated text. It is
  reserved for Phase 4's optional critic and planner assistance, behind Pydantic validation, and
  can only *choose among* deterministic candidates.
- **BM25 via an external engine.** PostgreSQL full-text ranking is adequate and adds no
  infrastructure.
