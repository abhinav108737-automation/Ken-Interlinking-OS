# ADR-0017 — Qdrant is the semantic retrieval layer; PostgreSQL stays the source of truth

- **Status:** Accepted, 2026-10-16. **Supersedes ADR-0003 and ADR-0010's choice of pgvector as the
  vector layer, and overrides CLAUDE.md #5 ("pgvector is the initial vector layer").**
- **Authority:** the user's final-completion directive of 2026-10-16, §2 ("Vector backend: Qdrant"),
  §3 ("Qdrant is the rebuildable semantic/vector retrieval layer… Do not maintain pgvector as a
  competing primary semantic backend") and §21. Qdrant was previously on the **forbidden** list; that
  prohibition is explicitly lifted by that directive. Neo4j, Redis, Kafka, Celery and Temporal remain
  forbidden.
- **Licence:** Qdrant server Apache-2.0; `qdrant-client` Apache-2.0. Both are free and self-hosted.
  No paid API, no managed service.

## Context

Phase 2 (ADR-0010) made `page_embeddings.embedding` a pgvector `vector` column and ran similarity
search in PostgreSQL (`services/ken_core/db/repositories/intelligence.py`, `cosine_distance` using
the `<=>` operator). That is the current production semantic path and it is exactly the "competing
primary semantic backend" the directive forbids.

## Decision

### 1. Responsibility split

**PostgreSQL is the source of truth for everything**, including the embedding *values*. Qdrant is a
**derived, rebuildable index**. The system must be able to drop the Qdrant collection and rebuild it
entirely from PostgreSQL, and a test proves it.

| Store | Holds |
|---|---|
| PostgreSQL | every table listed in the directive §3, including `page_embeddings` (the vector values, model, recipe version, content hash) |
| Qdrant | derived points only: the vector plus `page_id`, `page_version_id`, `chunk_id`, `content_hash`, embedding model and version, and retrieval metadata |

Nothing is stored **only** in Qdrant. Losing Qdrant costs a reindex, never data.

### 2. pgvector is removed, not merely bypassed

The directive says to remove remnants cleanly if safe. It is safe:

- migration `0010` converts `page_embeddings.embedding` from `vector` to `real[]`, keeping the
  existing `vector_dims`-style dimension check as a CHECK on `array_length`. Values are preserved.
- every similarity query in `repositories/intelligence.py` is deleted. `cosine_distance` and the
  `<=>` operator disappear from the codebase. A **static test asserts no `<=>` and no pgvector import
  remains**, so the competing path cannot come back by accident.
- `CREATE EXTENSION vector` is never issued. `KEN_REQUIRE_PGVECTOR` is removed from `Settings`, the
  readiness probe and `doctor`, replaced by a **Qdrant** probe. The Postgres image may stay
  `pgvector/pgvector:pg16` (it is a superset of `postgres:16` and switching images is a needless
  risk); the extension is simply never created or required. This finally makes the long-standing
  "native PostgreSQL without pgvector" degraded mode (ADR-0003) unconditional rather than special.

### 3. The contract

`contracts/python/ken_contracts/vectors.py` (new, frozen), async and keyword-only like every other
provider protocol:

```
VectorPoint{point_id, page_id, page_version_id, chunk_id | None, content_hash,
            embedding_model, embedding_version, vector, metadata}
VectorQuery{vector, limit, score_threshold | None, filters: VectorFilters}
VectorFilters{page_ids?, exclude_page_ids?, embedding_model?, embedding_version?,
              content_hashes?, page_types?, languages?}
VectorHit{page_id, page_version_id, chunk_id, score, content_hash, stale: bool}
CollectionInfo{name, vector_size, distance, points, status, embedding_model, embedding_version}

VectorStore(Protocol):
    ensure_collection(*, vector_size, distance) -> CollectionInfo
    upsert(*, points) -> UpsertResult          # idempotent on point_id
    search(*, query) -> list[VectorHit]
    delete_pages(*, page_ids) -> int
    delete_stale(*, page_id, keep_version_id) -> int
    collection_info() -> CollectionInfo
    health() -> ProviderHealth
    rebuild_from(*, points, batch_size) -> RebuildReport   # drop + recreate + fill
```

Implementations: `QdrantVectorStore` (HTTP via `qdrant-client`) and `MemoryVectorStore` (exact
cosine over a dict, the default in tests and CI so no container is needed). Chosen by
`build_vector_store(settings)`, mirroring the CMS and analytics factories.

**`point_id` is deterministic:** `uuid5(NAMESPACE_URL, f"{page_version_id}:{chunk_id or '-'}:{embedding_model}:{embedding_version}")`.
Re-indexing the same content therefore overwrites rather than duplicating, which is what makes
indexing idempotent.

### 4. Freshness and staleness are explicit

A hit is **stale** when its `page_version_id` is not the page's `current_version`, or its
`content_hash` differs from the stored one. Retrieval filters stale hits out by default; a stale hit
that survives a filter is marked `stale=True` and may never be used to place a link (deterministic
filter, §9 of the directive). `make qdrant-verify` reports drift both ways — points whose page is
gone or superseded (orphaned vectors) and current versions with no point (missing vectors).

### 5. Operations

- `make qdrant-up` / `qdrant-down` — the compose service (profile-free, like Postgres, since it is
  now part of the default local stack).
- `make qdrant-index [ARGS="--force"]` — idempotent incremental indexing from PostgreSQL.
- `make qdrant-rebuild` — drop the collection and refill from PostgreSQL.
- `make qdrant-verify` — consistency report, non-zero exit on drift.
- Readiness: a `qdrant` probe reports reachability, the collection, its point count and the
  embedding model/version it was built with. It is `required` when the semantic channel is enabled.

Compose adds the `qdrant/qdrant` image, pinned to an exact tag verified by pulling it at
implementation time, on **127.0.0.1:6333/6334** with a named volume. Ports 6333 and 6334 were
verified free on this machine, Colima/Docker 29.5.2 is running, and `qdrant-client` 1.19.1 is the
latest published version (Apache-2.0). Nothing binds 8765.

## Consequences

- Semantic retrieval moves out of SQL. The `SEMANTIC` channel calls `VectorStore.search`.
- `page_embeddings` stays immutable and append-only, so the index is always rebuildable.
- One new dependency (`qdrant-client`, Apache-2.0) and one new local container. Both are explicitly
  approved by the directive.
- Tests default to `MemoryVectorStore`, so the suite needs no container; Qdrant integration tests are
  marked and skip when it is unreachable.

## Alternatives considered

- **Keep pgvector and add Qdrant for scale.** Rejected: the directive forbids a competing primary
  semantic backend, and two retrieval paths means two sets of behaviour to keep honest.
- **Move the vectors out of PostgreSQL entirely.** Rejected: it would make Qdrant the source of truth
  for derived data that must be rebuildable, directly against §3.
- **Convert the column to `bytea`.** Rejected: `real[]` keeps the values readable and checkable in SQL
  without the extension.
