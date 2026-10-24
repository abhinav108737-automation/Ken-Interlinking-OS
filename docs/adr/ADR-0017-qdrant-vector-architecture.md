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
- Tests select `MemoryVectorStore` explicitly (`make_settings`), so most of the suite needs no container;
  Qdrant tests carry the `qdrant` marker and FAIL, not skip, under `make test-all` (hardening pass).

## Alternatives considered

- **Keep pgvector and add Qdrant for scale.** Rejected: the directive forbids a competing primary
  semantic backend, and two retrieval paths means two sets of behaviour to keep honest.
- **Move the vectors out of PostgreSQL entirely.** Rejected: it would make Qdrant the source of truth
  for derived data that must be rebuildable, directly against §3.
- **Convert the column to `bytea`.** Rejected: `real[]` keeps the values readable and checkable in SQL
  without the extension.

## Amendments settled during the contract freeze (2026-10-16)

1. **§2 was internally contradictory and is resolved as follows.** It said migration 0010 converts the
   column from `vector` to `real[]` *and* that `CREATE EXTENSION vector` is never issued; both cannot
   hold on a fresh chain, because 0003 created the `vector` column. Resolution: **migration 0003 was
   amended** to create `real[]` from the start with no extension, and 0010's conversion is **guarded
   by the live column type** — a fresh database skips it, an existing one converts. This edits a
   historical migration, which is normally avoided; it is safe here because the guard makes the
   upgrade path identical for any database already past 0003. **Verified by the orchestrator on a
   clone of the real dev database** (67 embeddings, previously stored as pgvector at 0008): upgrade
   through 0009 and 0010 left a byte-identical fingerprint of every vector, the downgrade restored a
   pgvector column with the same fingerprint, a second upgrade succeeded and `alembic check`
   reported no drift.
2. **§5 "required when the semantic channel is enabled".** `KEN_REQUIRE_QDRANT` defaults true, but a
   store that is reachable and merely not yet indexed reports `ok` with `collection_present: false`
   (the Phase 0 "provisioned, not enabled" precedent). Otherwise a fresh install would answer
   `/ready` 503 before anyone ran `make qdrant-index`. An unreachable or red store still makes the
   service not ready; drift is `make qdrant-verify`'s job.
3. **Freshness cannot live in the derived store.** `VectorFilters.include_stale` cannot be honoured by
   Qdrant, because "stale" depends on `pages.current_version` and `content_hash`, which live in
   PostgreSQL. Stores return `stale=False`; the retrieval layer re-checks against PostgreSQL before a
   hit may be used. This is a direct consequence of PostgreSQL being the source of truth.
4. **The Qdrant image is pinned to `qdrant/qdrant:v1.19.1`**, digest
   `sha256:12364fe851b9f17356fc88189fc06d1b521262e04659ec7345975b00c9246a10`, verified by pulling it,
   with the compose healthcheck reaching `healthy` and a live check that the deterministic `point_id`
   is idempotent (two upserts of one id produce one point).
5. **Non-additive renames:** `KEN_REQUIRE_PGVECTOR` → `KEN_REQUIRE_QDRANT`; the readiness check
   `pgvector` → `qdrant`; `ken_core.db.types.Vector`, `format_vector`, `parse_vector` and
   `repositories.intelligence.cosine_distance` are deleted. `CheckKind.EXTENSION` is retained so an
   old stored kind still parses.
6. **Licences.** `qdrant-client` 1.19.1 is Apache-2.0. Its transitive dependencies are `grpcio`
   (Apache-2.0), `protobuf` (BSD-3), `numpy` (BSD-3 AND 0BSD AND MIT AND Zlib AND CC0-1.0),
   `portalocker` (BSD-3) and `h2`/`hpack`/`hyperframe` (MIT) — all permissive. The package metadata
   was checked; libraries bundled inside binary wheels were not independently audited. `numpy` is now
   present transitively but is not used for any statistics (ADR-0016 still makes no significance claims).

## Corrections after integration (2026-10-17)

1. **The image is pinned by tag; the digest is recorded in a comment.** Item 4 above says "pinned to
   digest". `docker-compose.yml` pins `qdrant/qdrant:v1.19.1` and records
   `sha256:12364fe851b9f17356fc88189fc06d1b521262e04659ec7345975b00c9246a10` in a comment next to it.
   A tag can be re-pushed; pinning the digest in the `image:` line itself is a hardening for any
   shared environment.
2. **[Amended by the hardening pass: the code default is now `qdrant`; the test factories select `memory` explicitly. The original text follows.] `KEN_VECTOR_STORE` defaulted to `memory`, not `qdrant`.** The default is chosen so that the test
   suite and CI need no container. The consequence is that **a default installation does not use
   Qdrant**: it builds an exact in-process index per run, which is correct but not the production path.
   The Qdrant make targets (`qdrant-index`, `qdrant-rebuild`, `qdrant-verify`) pin
   `KEN_VECTOR_STORE=qdrant` themselves and the CLI warns on stderr when run against memory. An
   operator running the real pipeline must set `KEN_VECTOR_STORE=qdrant`; `.env.example` now says so.
3. **`KEN_REQUIRE_QDRANT` is a plain default-true flag.** Section 5 describes the probe as required
   "when the semantic channel is enabled"; the code does not condition it that way, and the memory
   store always probes `ok`. A reachable but not-yet-indexed Qdrant also reports `ok`
   (`collection_present: false`) so a fresh installation is not "not ready".
4. **The semantic backend is one of six generators.** ADR-0011 section 2 lists four channels using
   cosine search over page embeddings. The code has six independent generators (semantic, lexical,
   topic, graph, analytics, orphan recovery); semantic retrieval goes through `VectorStore.search`
   (this ADR), and the analytics and orphan-recovery generators are inert by default
   (`analytics_k` and `orphan_recovery_k` default to 0).

