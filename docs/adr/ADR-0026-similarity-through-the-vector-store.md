# ADR-0026 — Similarity at scale goes through the vector store (Qdrant), not an in-Python scan

- **Status:** Accepted and implemented, 2026-10-30.
- **Decision owner:** the project owner, 2026-10-30: use **Qdrant**, not `numpy`, because the engineering
  team's backend uses Qdrant and the project must comply with their stack.
- **Resolves:** NEXT_STEPS item 25. **Amends:** the Phase 2 semantic-relationship step (ADR-0010 section 4)
  and completes the direction of ADR-0017.
- **Authority:** CLAUDE.md. This changes verified Phase 2 code, so it carries an ADR and tests.

## Context

The semantic step of the analysis job (`_semantic_edges`) asked, for every page, for its nearest neighbours by
fetching **every** stored embedding from PostgreSQL and computing cosine similarity in Python. Measured on the
8,858-page inventory (768-dimension vectors): **2.3 s per page per pass**, so 6 to 11 hours for one run, inside a
single transaction that writes nothing until it ends. It is O(N) per page and O(N²) per run.

The recommender's semantic channel did **not** have this problem: since ADR-0017 it already searches through the
`VectorStore` abstraction (`ken_services/vectors/retrieval.py`). Only the relationship step had been left on the
old path.

## Decision

1. The semantic step uses the vector store when `KEN_VECTOR_STORE=qdrant`, or when a store is injected
   (`run_analysis(..., vector_store=...)`, used by tests). Otherwise it keeps the exact PostgreSQL scan, so the
   default (`memory`) behaviour and every existing test are unchanged.
2. Before searching, the job **indexes** PostgreSQL's current embeddings into the store (idempotent, incremental),
   so the derived index cannot lag the record. PostgreSQL stays the system of record (ADR-0017).
3. Neighbours come from `PostgresSemanticRetriever.similar_to_page`, which already drops stale hits. The result is
   re-sorted best first with ties broken by page id (reproducible, as before), self excluded, and restricted to
   pages that are valid candidates for this analysis version.
4. If the store is unavailable, the semantic edges are skipped and one error is counted
   (`semantic_index_failed`); page analyses and taxonomy edges are still written.
5. **`numpy` is not added** as a direct dependency.

## Evidence (real data, 2026-10-30)

| Check | Result |
|---|---|
| Recall@10 of Qdrant against the exact scan, 497-page corpus, 150 random pages | **1.000** (minimum 1.00) |
| Same, 8,858-page corpus, 60 random pages | **1.000** (minimum 1.00) |
| Latency per page, 8,858 pages | Qdrant **17 ms**; exact Python scan 488 ms in memory, 2,300 ms as the old code ran it |
| Indexing 497 pages / 8,858 pages | 12 s / about 100 s, drift report `in_sync: true`, 0 missing, 0 orphaned |
| **Full run on the real inventory**: `analyze --force`, taxonomy v3, 8,858 pages, Qdrant collection `ken_full` | **948 s (about 16 minutes) for the whole run** (taxonomy edges and semantic step), 0 errors; 88,282 semantic edges across 8,856 pages (about 10 each, the configured top-k). The old path was estimated at 6 to 11 hours for the semantic step alone |
| Tests | 4 new integration tests (same edges as the exact path; a new page replaces an old neighbour; a rerun changes nothing; an unavailable store costs only the semantic edges) plus the 36 existing analysis-pipeline tests, all passing |

The in-memory store is an exact-cosine oracle, so the new tests prove the wiring is equivalent; Qdrant's own
recall is the measurement above.

## Known differences and limits

- **Reverse-neighbour probe is capped at 500.** The incremental "whose top-k does this changed page enter"
  probe asked the exact scan for every neighbour above `min_score`; a store search returns at most 500
  (`MAX_SEARCH_LIMIT`). A **full** recompute is unaffected, and a very popular page in an incremental run could
  miss a distant neighbour's top-k refresh. Accepted; revisit if a corpus shows more than 500 above threshold.
- **Threshold boundary.** Qdrant's `score_threshold` and the exact scan may disagree on a score exactly equal to
  `min_score`; the downstream edge builder re-applies the threshold, so this cannot create a wrong edge.
- **Approximation.** Qdrant's HNSW search is approximate in general. Recall was 1.000 here; the collection could
  be configured for exact search if a corpus ever shows less.
- **One collection per corpus.** Use `KEN_QDRANT_COLLECTION` (for example `ken_real500`, `ken_full`) so a real
  corpus is never mixed with the demo points in `ken_pages`.

## Consequences

- NEXT_STEPS item 25 is resolved for the relationship step; the recommender needed no change.
- The `--no-embeddings` workaround is no longer needed to avoid the slow step.
- The default stays `memory`, so a fresh checkout and CI need no container.
