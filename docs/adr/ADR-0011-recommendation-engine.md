# ADR-0011 — Phase 3 recommendation engine: retrieve, filter, score, place

- **Status:** Accepted (Phase 3)
- **Date:** 2026-10-14
- **Contracts:** `ken_contracts/recommendations.py`. **Schema:** migration 0004. **Policy:**
  `policies/recommendation/recommendation.v1.yaml` and `policies/ranking/ranking_weights.v1.yaml`.
- **Revised before fan-out** after the architecture review: filter order, the split score threshold,
  placement safety, the lifecycle and transitions, immutability and indexes.

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

### 3. Filters and stages, in this order (the first rejection is recorded)

| Stage | Code | Rule |
|---|---|---|
| pair | `self_link` | target == source (also a DB CHECK) |
| pair | `source_protected` / `target_protected` | an active protection on either page. Checked **first**, so safety evals see it. |
| pair | `experiment_locked` | the `ExperimentGuard` says an active experiment forbids the pair (PRD 7.5). **Never fires before Phase 7**; the hook exists now. |
| pair | `source_ineligible` / `target_ineligible` | `compute_eligibility` (live): published, not gone, not noindex, canonical, HTTP 200, thick enough, linkable page type, fresh analysis |
| pair | `already_linked` | the source's **current** version already links to the target, via `existing_links` → `known_urls` (redirects and aliases count) |
| pair | `duplicate_recommendation` | an **open** plan exists for the pair, **or** a **live** plan (deployed / verified / measured / learning_captured) whose `source_page_version` ≥ the source's current version, meaning the inventory has not yet re-read the page that contains the link |
| pair | `previously_rejected` | a reviewer rejected this pair within `selection.rejection_cooldown_days` |
| pair | `page_type_incompatible` | the target type is not in `page_type_targets[source type]`, or intent compatibility is 0 |
| score | `below_min_relevance` | relevance below `selection.min_relevance` (cheap pruning, **before** placement) |
| place | `no_natural_anchor` | placement, attempted in rank order, found no supported anchor (§5): **nothing is ever fabricated** |
| place | `below_min_score` | the **final** score (relevance × placement quality) is below `selection.min_score` |
| select | `link_density_exceeded` | (existing internal body links + selected plans + 1) / words × 100 > `max_internal_links_per_100_words` |
| select | `per_source_limit` / `diversity_limit` | `max_plans_per_source` reached, or the target already has `max_plans_per_target_per_run` plans in this run |

### 4. Score

The score is transparent, every component is in [0, 1], and it can be checked from the record
alone:

```
relevance = Σ weight[c] × component[c]          (weights = ranking policy; Σ weights = 1)
total     = relevance × Π factors               (factors ∈ (0, 1]; today: placement_quality)
```

`ScoreBreakdown` validators enforce both identities. It also records the `semantic_floor` used.

| Component | Definition |
|---|---|
| `semantic` | clamp((cosine − floor) / (1 − floor)), where floor = `KEN_SIMILARITY_MIN_SCORE` (model-calibrated, recorded as `semantic_floor`). 0 without an embedding. |
| `taxonomy` | the maximum weight over the market-structure relationship types between source and target (`taxonomy_relationships`, live) |
| `intent` | `intent_compatibility[source intent][target intent]` |
| `graph` | source–target **complementarity** (PRD v1.1 §12.3): 1 / (1 + existing links from the source into the target's market) |
| `geography` | `geography_scores`: same country 1.0, target region contains a source country 0.6, same region 0.5, target global 0.3, else 0 |
| `commercial` | target `commercial_importance` / 3 |
| `underlinked` | max(0, 1 − inbound / `underlinked_inbound_threshold`), where inbound counts current internal links to the target |
| `historical` | 0 until Phase 8 provides calibrated outcome evidence. Until then relevance tops out at 0.97. |

**Interpretation, flagged for the user.** TRD §12 names the weight keys but does not define them.
The definitions of `graph` (complementarity) and `underlinked` (inbound scarcity) above are this
ADR's interpretation. `graph` is near-constant on sparse link graphs, so an ablation eval checks
its value.

`confidence` is the raw `total`, with `calibration_version = None`. **No threshold may treat raw
confidence as calibrated.** Phase 8 sets the calibration.

`reason_codes` are derived from the components and evidence. `rationale` is rendered from those
codes deterministically and never contains free text.

### 5. Placement (deterministic, never fabricated)

Blocks are the source version's `extract_blocks(body_html)` (Phase 1). Block ids equal the
normaliser's.

**Every block exposes its text RUNS:** the character spans of the block text, each with the tag
names of its ancestors (`ContentBlock.runs`). Placement depends on this.

1. **Anchor candidates for target T, in rank order:**
   - the aliases and label of T's `market_id` node (`target_topic`, at least `min_anchor_words`
     words);
   - then the aliases of T's other top topics at depth ≥ 1;
   - then T's title core (`target_title`) and two-word keyword bigrams (`target_keyword`). These
     two need at least `min_anchor_words_non_topic` (2) words.

   **Geography aliases are never anchors:** "India" must not link to a market page.
2. **Search:** blocks of an allowed kind (`placement.block_kinds`) for case-insensitive
   whole-phrase occurrences. The span must:
   - lie inside **one** text run;
   - have **no** ancestor in `placement.excluded_ancestors` (links, tables, quotes, figures, code,
     citations, sup/sub, headings), which also makes nested links impossible;
   - not overlap a span already selected for this source.

   A block is skipped if its existing links already reach `max_links_per_block`, or if it already
   links to T.
3. **Anchor rules:**
   - word count and minimum characters as configured;
   - never one of `forbidden_anchors`;
   - the text is copied **verbatim** from the block, in its original case.
4. **Choice:** the best (anchor source rank, phrase length descending, block salience descending,
   block order, occurrence). `placement_quality` is 1.0 for a topic alias, 0.9 for a title core and
   0.75 for a keyword bigram, minus 0.1 for the opening block.
5. **Locator:**
   - `block_id` (a **hint** only), `block_kind`, `occurrence_index`, and `start`/`end` in the
     **normalised** block text;
   - `context_hash` = sha256 of the full block text;
   - `preview` = up to 200 characters either side.

   **Phase 6 finds the block by (`context_hash`, anchor text, `occurrence_index`)**, because ordinal
   ids shift when blocks are inserted above.

### 6. Plans: immutable content, append-only history, freshness

- `link_plans` content is **immutable**. The trigger is **generic**: it rejects an UPDATE when
  `to_jsonb(NEW) - 'status' - 'updated_at'` differs from the same expression on OLD, so any column
  added later is immutable automatically. It also rejects DELETE.
- `link_plan_transitions` is append-only. It records every status change, with actor and time.
- **At most one open plan per (source, target)**, and per (source, block `context_hash`, anchor
  start) across versions. Both are partial unique indexes; overlapping spans are rejected in code.
- **The target version is pinned** by a composite foreign key to `page_versions`, as the source is.
- **`plan.id` is the stable identifier of the link.** It becomes `LinkChange.change_id` (Phase 6)
  and the analytics link id (Phase 7).
- **Status moves only along `ALLOWED_TRANSITIONS`**, through one transition service (tested).
  Terminal states are rejected, superseded, expired and rolled_back. An approval is never
  resurrected.
- **Re-runs are idempotent:** unchanged sources produce no new plans.
- **When a source gains a new version,** each open plan is checked with `check_plan_freshness`:
  - **stale** plans move to `expired`, and fresh plans are generated;
  - **rebasable** plans keep their status, including human approvals, and are rebased at deploy
    time.

  A CMS version conflict (409) also means `expired`.
- **An edit (Phase 5)** moves the old plan to `superseded` and inserts the new plan in the **same
  transaction**. `anchor_source = "edited"` must still be copied verbatim from the block text. A
  partial unique index on `supersedes` allows at most one successor.
- **`check_plan_freshness(plan, current version, current blocks)`** returns one of:
  - `fresh`: same version;
  - `rebasable`: new version, but the same block text (context hash) and the anchor still at its
    offsets;
  - `stale`: the plan must be regenerated.

  Phase 6 refuses to mutate a stale plan (PRD §13 "version awareness").
- **Events:** `link.proposed` per new plan (`LinkPlanEvent`, `subject_type = link_plan`) and
  `job.completed` per run.

### 6a. Full-text column

`page_versions.search_vector` is a STORED generated `tsvector` built from:
- the title (first 2,000 characters);
- the description (first 5,000);
- the body (first 100,000).

The caps keep inserts under PostgreSQL's 1 MB tsvector limit. The column is deferred in the ORM,
because it is queried in SQL only. Adding it rewrites the table, which fires no row trigger, so it
is compatible with the append-only rule.

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
