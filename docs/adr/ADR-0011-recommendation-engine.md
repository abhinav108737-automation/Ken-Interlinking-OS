# ADR-0011 — Phase 3 recommendation engine: retrieve, filter, score, place

- **Status:** Accepted (Phase 3)
- **Date:** 2026-10-14
- **Contracts:** `ken_contracts/recommendations.py`. **Schema:** migration 0004. **Policy:**
  `policies/recommendation/recommendation.v1.yaml` and `policies/ranking/ranking_weights.v1.yaml`.
- **Revised before fan-out** after the architecture review: filter order, the split score threshold,
  placement safety, the lifecycle and transitions, immutability and indexes.
- **Revised during integration** (§9): the self-describing-anchor rule, sub-market-only secondary
  topics, excluded target paths, deterministic source order, the open-plan diversity cap,
  scale-free candidate ranking and freshness details.

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

Sources are processed in **canonical-URL order** (then id), one transaction each. Before
generating, every OPEN plan of the source is re-validated: a plan whose source or target is now
DURABLY ineligible (protected, gone, unpublished, noindex, canonical alias, thin, excluded page
type), or whose pair is now already linked, moves to `expired` (`invalid: …`, fail closed; QA
H2). Analysis lag (`stale_analysis`, `not_analyzed`) and `http_not_ok` (possibly transient) only
pause generation: expiry is terminal and would destroy approvals over an operational ordering
slip (QA re-review). Deployment re-validates everything (Phase 6).
Other open plans on an older source version get the freshness check (§6).

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
| pair | `target_excluded` | the target path matches `excluded_target_paths` (default `^/$`, the home page) |
| pair | `page_type_incompatible` | the target type is not in `page_type_targets[source type]`, or intent compatibility is 0 |
| score | `below_min_relevance` | relevance below `selection.min_relevance` (cheap pruning, **before** placement) |
| place | `no_natural_anchor` | placement, attempted in rank order, found no supported anchor (§5): **nothing is ever fabricated** |
| place | `below_min_score` | the **final** score (relevance × placement quality) is below `selection.min_score` |
| select | `link_density_exceeded` | (existing internal body links + selected plans + 1) / words × 100 > `max_internal_links_per_100_words` |
| select | `per_source_limit` / `diversity_limit` | `max_plans_per_source` OPEN plans of the source reached, or the target already has `max_open_plans_per_target` OPEN plans from any source (seeded from the database at run start) |

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
   - then the aliases of T's other CONFIDENT top topics at depth ≥ 1 that are **sub-markets of
     T's own market** (§9.2);
   - then T's title core (`target_title`) and two-word keyword bigrams (`target_keyword`). These
     two need at least `min_anchor_words_non_topic` (2) words.

   **Geography aliases are never anchors:** "India" must not link to a market page. A phrase
   that names the SOURCE's own market — or is a word fragment of one of its names — carries
   `context_terms` (§9.1).
2. **Search:** blocks of an allowed kind (`placement.block_kinds`) for case-insensitive
   whole-phrase occurrences. The span must:
   - lie inside **one** text run;
   - have **no** ancestor in `placement.excluded_ancestors` (links, tables, quotes, figures, code,
     citations, sup/sub, headings, form labels/legends, struck or small print, media fallback
     content, header/footer/dialog/address/details/fieldset regions), which also makes nested
     links impossible;
   - not overlap a span already selected for this source;
   - not be a strict **fragment** of a longer market label/alias or anchor phrase occurring
     around it ("battery energy" inside "battery energy storage"; QA H1);
   - for a phrase with `context_terms`, sit in a block that also names one of them.

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
- **Actor authority (`TRANSITION_AUTHORITY`, QA H3).** The actor's class must hold authority
  over the target status: only `user:` approves, rejects or supersedes; only `system:` (the
  engine, the deployment service) validates, deploys, verifies, fails, rolls back, expires or
  measures; `agent:` may only critique, policy-check and queue for approval. An agent never
  decides. That a `user:` actor is AUTHENTICATED is the API's job (Phase 5 gate): the service
  trusts its caller.
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
    offsets; `FreshnessCheck.block_id` names the matched current block;
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

### 9. Decisions taken during integration (2026-10-14, after the fixture evals)

1. **No self-describing anchors.** This is the most consequential decision, recorded here because
   it changes recall.
   - **The rule.** A phrase that names the **source page's own market** (its label or an alias)
     describes the page the reader is already on. On the India cold-chain report, "cold chain"
     must not link to the UAE cold-chain report (PRD v1.1 §12.4: no misleading anchors; the
     destination must fit the reader journey).
   - **The exception.** Such a phrase is kept only with `AnchorPhrase.context_terms`, the target's
     **distinctive** geographies: countries and their regions that the source does not share. It
     is then a valid anchor only in a block that also names one of them, for example
     "… electric vehicles in Thailand …" linking to the Thailand report. When nothing
     distinguishes the target, the phrase is dropped.
   - **The commercial-hop exception.** An article (non-commercial intent) linking to the
     commercial-research report on the same market is the core reader journey, from discussion
     to the authoritative report, and keeps the plain market phrase ("cold chain" in an India
     vaccine cold-chain article → the India cold-chain report).
   - **Fragments count.** A title core or keyword bigram that is a word sub-span of the source's
     market names ("Battery Energy" on a battery-energy-storage page) is treated like the name.
     Topic phrases match exactly: a parent market's label ("logistics") may share a word with an
     own alias ("temperature-controlled logistics") and still name a different page.
   - **The hop is from an INFORMATIONAL page** (an insight or article) only; navigational and
     transactional pages are not articles.
   - **Per block, not per sentence.** The context term may be anywhere in the same paragraph or
     list item.
   - **The cost, measured, and the labels.** On the synthetic demo corpus, recall of the
     ORIGINAL GOOD set fell from 0.92 to 0.20: most of those pairs expected exactly the shared
     market name. QA (M3) found that the labels contradicted the rule. The 45 affected pairs were
     relabelled ACCEPTABLE by a deterministic rule (R5 in the judgments file, tagged in `why`);
     they are still judged positives, so precision is unaffected. The recall floor was
     **restored** to 0.70 on the remaining 30 GOOD pairs. Measured: mock precision@3 0.98,
     recall 0.78; local nomic precision@3 1.00, recall 0.80, BAD rate 0.
   - **Where those relationships still appear.** They remain visible as `same_market` edges on
     every page's Intelligence panel.
   - **How to recover them:** text that names the target's country or region near the market
     phrase. Editors can add it, or Ken's real pages may already have it.
   - **Approval status.** Orchestrator decision, **flagged for product-owner review**
     (NEXT_STEPS): whether same-market, cross-country pairs deserve a plain market-name anchor is
     editorial, and the rule is one policy switch away from either answer.
2. **Secondary-topic anchors** are restricted to **sub-markets of the target's own market**.
   Otherwise a passing mention, such as a cold-chain report noting "active pharmaceutical
   ingredients", names another page's market.
3. **`excluded_target_paths`** (policy; default `^/$`) is a list of paths that are never link
   targets. The home page is a listing, and a taxonomy match on it is an artefact ("mobility" must
   not link to `/`). These targets get their own rejection code, `target_excluded`.
4. **Sources are processed in canonical-URL order**, then by id. Caps are consumed in that order,
   so random page UUIDs must not decide which source gets a contested target. With the
   deterministic provider, two independent ingestions now produce identical plans. With a real
   model, floating-point noise in local embeddings can flip a near-tie in the nearest-neighbour
   search (1 of 46 plans on the demo).
5. **Destination diversity counts every OPEN plan pointing at the target,** from all sources. The
   count is seeded from the database at the start of the run, which is what makes re-runs
   idempotent. The policy key keeps its name, `max_open_plans_per_target`; its description
   states the semantics.
6. **The candidate cap ranks scale-free.** Each channel's scores are normalised by that channel's
   best score for the source, and candidates are ranked by the sum of those normalised scores.
   `ts_rank_cd` values of 3–8 would otherwise crowd out cosines up to 1. Raw scores are stored.
7. **`FreshnessCheck.block_id`** returns the matched current block, so Phase 6 can relocate the
   anchor without repeating the search.
8. **Policy v1 was amended in place before release.** `recommendation.v1.yaml` and migration
   0004 were edited during Phase 3 integration (rename to `max_open_plans_per_target`, the
   `target_excluded` code, the wider `excluded_ancestors`). No plan existed outside local
   development and test databases, which were rebuilt. **From the Phase 3 tag on, v1 and 0004
   are immutable:** a change is a new policy version and a new migration.
9. **Freshness of an edited block:** when the block's text changed and the anchor is no longer at
   its offsets, both `context_changed` and `anchor_moved` are reported.
10. **Placement details** (from the core implementation):
   - `ContentBlock.enclosing` holds the tags around the block element, so `<td><p>…` and
     `<blockquote><p>…` blocks are rejected, as are spans inside inline excluded tags.
   - Phrase boundaries exclude word characters **and hyphens**, so "ion" never matches inside
     "li-ion".
   - `occurrence_index` counts case-insensitive whole-phrase occurrences (`phrase_occurrences`).
   - The opening-block penalty applies to the first non-empty block of an allowed kind, which is
     the opening paragraph rather than the h1.
   - Title cores strip a trailing "Market/Report/Outlook/Industry…" tail and leading or trailing
     geographies.
   - The `taxonomy` component counts `industry_adjacency`.
   - A target with no country or region counts as global.

### 10. Known limitations (recorded, not blocking)

- **Scale.** The semantic channel is an exact KNN scan per source (O(N²) per run) and the graph
  channel uses a correlated inbound-count subquery. Fine for thousands of pages; for Ken's full
  site add an HNSW/ivfflat index (pgvector, no new infrastructure) and reuse `world.inbound`.
- **Status integrity in the database.** The content trigger guards content, but a direct
  `UPDATE link_plans SET status = …` by the application role is still possible. Only the
  transition service writes status today; DB-role hardening (status only via a SECURITY DEFINER
  function) is a Phase 6 item.
- **Inline `style="display:none"`** is not detected (the `hidden` and `aria-hidden` attributes
  are). Phase 6 post-publish verification renders the page.
- Adding the stored `search_vector` column rewrites `page_versions` under an ACCESS EXCLUSIVE
  lock: an operational note for a large production table.

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
