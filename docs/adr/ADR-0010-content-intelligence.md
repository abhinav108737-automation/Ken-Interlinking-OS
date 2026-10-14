# ADR-0010 — Phase 2 content intelligence: taxonomy, embeddings, relationships

- **Status:** Accepted (Phase 2)
- **Date:** 2026-10-14
- **Phase:** 2
- **Contracts:** `ken_contracts/intelligence.py`, `ken_contracts/jobs.py`. **Schema:** migration 0003.
- **Revised 2026-10-14 (during integration, after the fixture evals and the final architecture
  review):**
  - (a) `market_id` is the highest-scoring confident node (§3).
  - (b) The `semantic_similar` threshold is model-specific; the default is 0.78 (§4).
  - (c) CMS protection is mirrored by ingestion on every document, and the `cms-protected` policy
    rule was removed from taxonomy v1 before any production use (§5).
  - (d) Static, typed imports between the intelligence modules.
  - (e) After the QA review: the coarse relationship types are derived, not stored, and stored
    taxonomy edges are capped per (source, type) (§4).
  - (f) The embedding recipe fingerprint is part of embedding identity (§2).
  - (g) Protection paths are matched in canonical form (§5).

## Context

Phase 2 must represent meaning, structure and topical relationships in forms usable for retrieval
(IMPLEMENTATION_PLAN.md, PRD v1.1 §12.1, FR-004). That covers:
- taxonomy, intent, entities and topics;
- summaries and salient blocks;
- embeddings with pgvector;
- typed page relationships (PRD §7.3);
- protected-page flags;
- the eligibility classification Phase 3 needs.

The governing rules: deterministic before agentic; the embedding dimension comes from the provider
and is never hard-coded; no paid API; no new infrastructure.

## Decision

### 1. pgvector becomes required from migration 0003

- **Migration 0003 runs `CREATE EXTENSION IF NOT EXISTS vector`** in the same migration that creates
  the first vector column, `page_embeddings.embedding`. This is exactly what ADR-0003 deferred.
- **`KEN_REQUIRE_PGVECTOR` now defaults to `true`.**
- **Native degraded mode narrows to a schema guarantee.** A PostgreSQL without pgvector can still
  apply migrations up to 0002. The Phase 2 build then reports `/ready` **503** against it, because
  its migrations are behind head. Migration 0003 fails there loudly, with pgvector's own error; it
  never silently skips.
  - The CI job asserts both: 0002 applies, and 0003 fails *because* the extension is missing.
  - ADR-0003 is amended accordingly.
  - PRD v1.1 §17.1 ("preserving a genuine native degraded mode") holds only for that schema range.
    This is flagged for the next PRD revision.

### 2. Embeddings

- **Provider:** the existing `ModelProvider.embed(role=EMBEDDING)`. No second abstraction is added.
  - The default local model is **`nomic-embed-text`** (Apache-2.0, 137 M parameters), pulled with
    `ollama pull nomic-embed-text`. It is configured as `KEN_MODEL_EMBEDDING`, never named in code.
  - Its task prefixes (`search_document:` / `search_query:`) are **configuration**
    (`KEN_EMBEDDING_*_PREFIX`), not business logic.
- **Dimension:** read from `EmbeddingResult.dimensions` at write time and stored per row.
  - The column is a **dimension-less** `vector`.
  - A CHECK ties `vector_dims(embedding)` to `dimensions`.
  - Every similarity query filters on (model, dimensions), so vectors from two models are never
    compared.
  - No code hard-codes 768.
- **What gets embedded:** one vector per page version, from a deterministic text recipe
  (`EMBEDDING_RECIPE_VERSION`): title, description, headings and body text up to
  `KEN_EMBEDDING_MAX_CHARS`.
  - **Embedding identity** is the **provider-reported** model: `ProviderCapabilities.roles[EMBEDDING]
    .model`, which must equal `EmbeddingResult.model`. It is combined with the provider name,
    dimensions and recipe version, and resolved by one helper (`embedding_identity`). It is never
    the raw setting string, which can differ, as with `nomic-embed-text` vs `:latest` or the mock's
    own name.
  - **The recipe settings are part of identity too.** `recipe_fingerprint` is
    `r{EMBEDDING_RECIPE_VERSION}-` plus the first 16 hex characters of the sha256 of
    (recipe version | document prefix | max chars).
    - It is stored per row, included in the unique key, and filtered on by every similarity query.
    - Changing `KEN_EMBEDDING_DOCUMENT_PREFIX` or `KEN_EMBEDDING_MAX_CHARS` therefore makes existing
      vectors stale. They are re-embedded as new rows and never mixed with the old ones (QA M1).
  - An embedding is **fresh** when a row exists for (current version, provider-reported model,
    dimensions, recipe version, current fingerprint).
  - `input_hash` detects whether the recipe text itself changed.
- **Mock embeddings** (tests, CI, Mode B without Ollama) switch from hash-seeded random vectors to
  **feature hashing**: token uni- and bigrams hashed into the configured dimension, then
  L2-normalised.
  - They are still fully deterministic, and now *lexically meaningful*: related pages really are
    closer.
  - That lets retrieval tests and evals assert similarity behaviour without a model.
- **Search:** exact cosine KNN (`<=>`) over current versions.
  - At the scale of a research site (thousands of pages) this is milliseconds, and it needs no
    index tuning.
  - An HNSW/IVFFlat index needs a fixed dimension. It is deferred until measured latency requires
    it. It would then be a partial expression index per (model, dimensions), added by migration.
- **The `pgvector` Python package is not needed.** asyncpg exchanges the extension type in its text
  form; `ken_core.db.types.Vector` handles the conversion. This was verified against the project
  image.

### 3. Deterministic analysis from a versioned taxonomy policy

`policies/taxonomy/taxonomy.v1.yaml` is the data that drives classification. It is validated on load
by `TaxonomyPolicy`, which checks unique ids and that every reference resolves. It contains:
- the market tree (top level = industries) with aliases and relationship declarations;
- the geography tree (global → region → country);
- an entity gazetteer;
- page-type rules (URL path regexes → page type, intent, commercial importance, linkability);
- protection rules;
- eligibility thresholds.

v1 is an **illustrative seed** (`source: fixture`), not Ken's official taxonomy.

The analyser is code over that data. It does not call an LLM.

| Output | Method |
|---|---|
| page type, intent, commercial importance | first matching page-type rule on the URL path |
| topics (markets) | whole-phrase alias matching, weighted by location (title 5, headings 3, description 2, body 1, body capped at 10 mentions per node). `market_id` is the **highest-scoring** node whose score clears the confidence threshold (3.0); ties go to the deeper node, then the id. `industry_id` is its root. (The original wording, "the most specific confident node", agrees on every report and insight in the demo corpus. It differs only on hub pages that list many sub-markets, where the industry is the right answer. The fixture eval settled it.) |
| geographies | the same matcher over the geography tree, except that `global` is never listed ("global market" is generic phrasing, not a geography) |
| entities | gazetteer matching with mention counts |
| keywords | top terms by a deterministic within-page salience score (stopwords removed) |
| summary | extractive: the leading body sentences, capped at 600 characters |
| salient blocks | the body blocks with the highest density of topic and keyword matches |

`page_analyses` rows are **immutable** (they join the Phase 1 append-only trigger). There is one per
(page version, `analysis_version`), where `analysis_version = "tax{N}-alg{M}"`. A new taxonomy or
algorithm version produces new rows, and the old rows remain reproducible. LLM-derived annotations
are Phase 4's understanding agent's job: validated, marked `extractor="llm"` and optional.

### 4. Relationships are derived and replaceable

`page_relationships` stores typed directed edges (PRD §7.3, plus `semantic_similar`) with a weight
in [0, 1] and structured evidence (shared node ids, cosine score; never free text). The edges are
computed only between pages with fresh analyses, from:
- taxonomy (same, parent, child and sibling market; geography containment; declared supply-chain,
  complementary and adjacency links);
- the top-k embedding neighbours above `KEN_SIMILARITY_MIN_SCORE`.

They are derived data, so this table, unlike the immutable history tables, is mutable by design.
- **Only specific relationships are stored.** The QA review measured unbounded growth:
  - about 1 million edges and 347 MB for 1,072 synthetic pages;
  - 1,897 out-edges for a single page;
  - most of them pure-geography edges.

  Two rules follow:
  - **`same_geography`, `regional_parent` and `industry_adjacency` are never stored.** They remain
    relationship types, and Phase 3 computes them per candidate pair with
    `taxonomy_relationships()`.
  - **Every stored taxonomy type is capped per (source page, type)** at
    `KEN_RELATIONSHIP_MAX_PER_TYPE` (default 25). The kept edges are ranked by weight, then the
    number of shared geographies, then target id. Under a cap, a page's kept set depends on all of
    its candidates, so a run recomputes the full out-edge set of every affected source. A test
    proves an incremental run yields exactly the graph a forced run would.
  - The 65-page demo now stores 514 taxonomy edges, down from 3,531, with a maximum out-degree
    of 19.
- **Each edge records both endpoint versions** (`source_page_version`, `target_page_version`).
- **A run replaces every edge touching a page it analysed, as source *or* target.** Without that,
  edges from unchanged pages would keep pointing at a stale analysis.
- **For `semantic_similar`,** the run also re-evaluates the existing neighbours' top-k against newly
  analysed pages, so a new page can enter an old page's neighbour list.
- **The cosine threshold `KEN_SIMILARITY_MIN_SCORE` is model-specific, not universal.**
  - On the demo corpus, `nomic-embed-text` gives related pairs about 0.85 and unrelated pairs about
    0.75. At the original 0.55, 99% of all pairs qualified. The default is therefore **0.78**.
  - The deterministic mock's cosines are lower; use about 0.3 with it.
  - The value is recorded in every run's configuration snapshot. It must be re-calibrated
    (`evals/intelligence/benchmark.py`) whenever the embedding model changes.
  - Binding the threshold to the embedding identity automatically is a later improvement.
- **Readers treat an edge whose endpoint version is no longer current as stale** and exclude it.
- **No table may foreign-key `page_relationships.id`.** Phase 3 copies the evidence values it
  relies on into its own immutable records.

### 5. Protection and eligibility

- **The CMS's own flag is mirrored at ingestion, not by a policy rule.** On EVERY document,
  changed or not, ingestion syncs a `source: cms` protection from the `cms_protected` assertion.
  Protection must never lag, and metadata is outside the content hash, so a CMS switching
  protection on for unchanged content creates no new version. The sync is asymmetric:
  - any CMS-derived record (a CMS read or a CMS export) may **add** the protection;
  - only an **authoritative live CMS read** may **clear** it;
  - a stale or partial export, or an alias record, never removes it.

  The original `cms-protected` policy rule read the immutable version metadata, which made it
  "sticky" after the CMS cleared the flag. It was removed so that each fact has one mechanism.
- **Protection path rules match a canonical path:** percent-decoded, `;params` removed and
  case-folded. `/Privacy`, `/privacy;jsessionid=1` and `/%70rivacy` therefore cannot fail open
  (QA H2). The v1 legal-pages pattern also covers locale prefixes, `-policy`, `-of-use` and
  `.html` variants.
- **`page_protections`:** protections come from three sources:
  - policy rules (path or metadata, for example the CMS adapter's `cms_protected` metadata);
  - the CMS itself;
  - operators (manual, Phase 5).

  They are **deactivated, never deleted**, and at most one is active per (page, source, rule). Any
  active protection blocks every future mutation of, or link to, that page.
- **Eligibility is computed at read time, never stored.** `LinkEligibility` checks, from current
  state:
  - publication state;
  - `gone_at`;
  - active protections;
  - robots `noindex`;
  - canonical status (aliases are never targets);
  - HTTP status;
  - word-count thresholds;
  - page-type linkability;
  - analysis freshness.

  Stored eligibility would go stale the moment any of those changes. Phase 3 filters candidates
  with the same function.

### 6. Generic job runs and the `job.completed` event

`job_runs` (kind, status, actor, timings, counters, secret-free snapshot, safe failure reason) records
every batch job from Phase 2 on: analysis, recommendation, agent batches, verification, analytics
import, evaluation and autonomy shadow runs. It avoids one bespoke run table per phase. One run
per kind runs at a time (session advisory lock). Ingestion keeps `ingestion_runs` and
`crawl.completed`.

**New governed event name: `job.completed`** (CONTRACTS rule 12). It is emitted once per finished
job run, including failed ones, with `JobCompletedPayload`. No existing name fits a non-crawl batch
job. The alternative, emitting `crawl.completed` for analysis runs, would make that name lie.

### 7. API and UI

The routes are read-only `GET /intelligence/*` and `GET /jobs/runs*`. `/intelligence/search` embeds
the query at request time and answers **503 `model_unavailable`** when the model cannot be reached.
It stores nothing. Analysis runs start from the CLI (`make analyze`).

### 8. Mapping to TRD §5's table names

TRD §5 lists `page_entities` and `graph_edges`. They are realised as:
- `page_entities` → `page_analyses.entities` (JSONB, GIN-indexed, versioned with the analysis);
- `graph_edges` → `page_relationships` (typed, weighted, evidenced, version-stamped edges).

`page_embeddings` keeps its TRD name.

## Consequences

- Phase 3 retrieval can combine lexical, embedding, taxonomy and graph channels, each ablatable.
- Re-analysis runs only for pages whose current version lacks a fresh analysis or embedding.
  Unchanged pages cost nothing.
- Ken's real taxonomy is required for production-quality classification (NEXT_STEPS).
- An exact KNN scan is O(pages) per query. An ANN index is a measured future decision.

## Alternatives considered

- **An LLM classifier for taxonomy and intent.** It is non-deterministic, slow on a laptop, and
  unnecessary for dictionary-style market and geography tagging. It is reserved for Phase 4, as a
  validated, optional annotation layer.
- **A fixed-dimension `vector(768)` column.** It would hard-code one model's dimension into the
  schema and need a migration to change models.
- **Qdrant or another vector database.** It is forbidden without evidence (TRD §2), and pgvector
  suffices at this scale.
- **The `pgvector` Python package.** It is MIT and fine, but unnecessary given asyncpg's text I/O.
  One less dependency.
