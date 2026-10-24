# Qdrant architecture

Reference for engineers operating the vector layer. Every statement was checked against the module cited; unverified items are marked. Governing decision: ADR-0017 with its contract-freeze amendments.

## Responsibility split

PostgreSQL is the source of truth for everything, including embedding values. Qdrant is a derived, rebuildable index. Nothing is stored only in Qdrant; losing the collection costs a reindex, never data.

| Store | Holds |
|---|---|
| PostgreSQL | `page_embeddings`: the vector as `real[]`, model, recipe version and fingerprint, the input content hash, the dimension (CHECK `array_length(embedding, 1) = dimensions`). Also pages, versions and `pages.current_version`. |
| Qdrant | One cosine collection (default name `ken_pages`, `KEN_QDRANT_COLLECTION`) of points: the vector plus payload `page_id`, `page_version_id`, `chunk_id`, `content_hash`, `embedding_model`, `embedding_version`, `metadata`, and `page_type` / `language` when present (`services/ken_core/providers/vectors/qdrant.py`). Keyword indexes are created on the filterable fields. |

The contract is `contracts/python/ken_contracts/vectors.py` (`VectorStore`, `VectorPoint`, `VectorQuery`, `VectorFilters`, `VectorHit`). `build_vector_store(settings)` in `services/ken_core/providers/vectors/factory.py` selects the implementation from `KEN_VECTOR_STORE`, which defaults to `qdrant`; `memory` is selected explicitly by test factories only. Selecting `qdrant` never falls back silently to memory. A comment in `factory.py` still says the Qdrant module is "a separate workstream" that may be absent; it exists and is used.

## Deterministic `point_id`

`point_id()` in `ken_contracts/vectors.py`: `uuid5(NAMESPACE_URL, f"{page_version_id}:{chunk_id or '-'}:{embedding_model}:{embedding_version}")`. `page_version_id` is `<page_id>:<version>` (`services/ken_services/vectors/identity.py`) and `embedding_version` is the recipe fingerprint. Re-indexing identical content overwrites a point; a new page version or embedding recipe gets a new id. The indexer, rebuilder, verifier and semantic generator all build points through `point_from_embedding`, so they cannot disagree.

## Freshness is decided in PostgreSQL

Stores always return `stale=False` (`qdrant.py`, `memory.py`) because staleness depends on `pages.current_version` and the stored content hash, which a derived store cannot know. `services/ken_services/vectors/retrieval.py` (`check_freshness`) re-stamps every hit: stale if the page is gone or unknown, the hit's `page_version_id` is not the current version, or its `content_hash` differs from the stored embedding's. Stale hits are dropped by default (`VectorFilters.include_stale` keeps them for inspection). The recommendation semantic generator (`recommendations/generators.py`) over-fetches and applies the same check. At decision time, `STALE_VECTOR` in the autonomy engine is computed from PostgreSQL embedding rows for the current versions of source and target, not by querying Qdrant (`autonomy/facts.py`).

## Indexing, rebuild, verify

All code is in `services/ken_services/vectors/`.

- **Index** (`index.py`, `PostgresVectorIndexer`): resolves one embedding identity (model and recipe fingerprint, `identity.py`), reads current-version embeddings of live pages, ensures the collection with the vectors' actual dimension (most common dimension wins; the rest are reported as `dimension_mismatch`), scans existing points, upserts only missing points or those with a changed content hash (`--force` sends all), deletes superseded versions of touched pages, and deletes points of gone pages. Zero vectors are skipped. Each pass is a `vector_index` job run with an advisory lock.
- **Rebuild** (`PostgresVectorRebuilder`): `rebuild_from` drops the collection, recreates it and refills from PostgreSQL in batches.
- **Verify** (`verify.py`, read-only): reports in both directions. `missing_vectors`: a current embedded version with no point. `orphaned_points`: a point whose page is gone or unknown or whose version is superseded. It additionally reports `stale_points` (current version, hash mismatch), `identity_mismatch` and `dimension_mismatch`. The CLI exits non-zero unless all are clear (`vectors/cli.py`).
- A collection with a different vector size or metric is never recreated silently; it raises, and an operator rebuilds.
- The recommendation job (`recommendations/pipeline.py`) runs one incremental index pass per run into whichever store is configured, when an embedding provider is reachable. With the memory store (tests only) that fills a process-local index. Without a shared store, `generators.py` falls back to an exact in-process index built from PostgreSQL.

## `MemoryVectorStore`

`services/ken_core/providers/vectors/memory.py`: exact cosine over a dict keyed by `point_id`, process-local, used by tests and CI (selected explicitly) so no container is needed. It is a store, never a source of truth, and is rebuilt through `rebuild_from`. It implements the same protocol; a shared contract test suite runs against it (`tests/unit/vectors/test_store_contract.py`). Qdrant integration tests carry the `qdrant` marker and need the running container: they skip with a loud reason by default and FAIL under `KEN_REQUIRE_QDRANT_TESTS=1` (set by `make test-all`/`test-qdrant`).

## Readiness

`services/ken_core/health/probes.py`, `probe_vector_store`:

- unreachable store: unavailable, so `/ready` fails while `KEN_REQUIRE_QDRANT` (default true) is set;
- reachable but collection missing (nothing indexed yet): `ok` with `collection_present: false, indexed: false`, so a fresh install is ready before the first `make qdrant-index`;
- collection status red: unavailable, with advice to run `make qdrant-rebuild`;
- otherwise `ok`, with point count and embedding model and version.

With the memory store the probe is `ok` (the store reports healthy). Drift is not a readiness concern; it is `make qdrant-verify`.

## pgvector removal and the migrations

pgvector is removed, not bypassed. `CREATE EXTENSION vector` is not issued by the current migration chain, and `tests/unit/test_vector_extension_removed.py` fails if `<=>`, `cosine_distance`, a pgvector import or `CREATE EXTENSION vector` reappears under `services/`, contracts, API, agents or migrations. `KEN_REQUIRE_PGVECTOR` became `KEN_REQUIRE_QDRANT`. The compose Postgres image is still `pgvector/pgvector:pg16` (a superset of `postgres:16`); the extension is simply never created.

- **Migration 0003** (`0003_content_intelligence.py`) was amended: it creates `page_embeddings.embedding` as `real[]` directly, with no extension, and the CHECK `array_length(embedding, 1) = dimensions`. This edits a historical migration.
- **Migration 0010** (`0010_qdrant_and_autonomy.py`) converts the column only if its live type, read from `pg_attribute`, is not `real[]`. A fresh database skips the conversion; a database that ran the original 0003 holds a `vector` column and is converted with its values preserved. The downgrade recreates a `vector` column only if the extension exists, so a database whose 0003 ran after the amendment correctly stays `real[]`. The ADR records a fingerprint check of 67 real embeddings round-tripping; that check was not re-run for this document.

## Pinned image and licences

`docker-compose.yml` pins `qdrant/qdrant:v1.19.1`, digest `sha256:12364fe851b9f17356fc88189fc06d1b521262e04659ec7345975b00c9246a10` (recorded in the compose comment; the service line itself uses the tag, not the digest). It publishes `127.0.0.1:6333` (HTTP) and `127.0.0.1:6334` (gRPC) only, uses the named volume `qdrant-storage`, and has a bash `/dev/tcp` healthcheck against `/readyz`. Python client: `qdrant-client==1.19.1` (`pyproject.toml`). `KEN_QDRANT_URL` must be loopback or the `qdrant` compose service name in local and CI environments (`config.py`).

Licences: `qdrant-client` 1.19.1 is Apache-2.0, verified from the installed package metadata (`License: Apache-2.0`). The Qdrant server is Apache-2.0 per ADR-0017, `pyproject.toml` and the compose comment; the image's licence was not independently inspected. Transitive dependency licences are as recorded in ADR-0017 amendment 6 and were not re-audited here.

## Operating commands (Makefile)

All exist and all pin `KEN_VECTOR_STORE=qdrant` except `qdrant-up`/`qdrant-down`:

| Command | Effect |
|---|---|
| `make qdrant-up` | start the container and wait for healthy |
| `make qdrant-down` | stop it; the volume is kept |
| `make qdrant-index [ARGS="--force"]` | idempotent incremental index from PostgreSQL |
| `make qdrant-rebuild` | drop and refill from PostgreSQL |
| `make qdrant-verify` | two-directional drift report; non-zero exit on drift |

Exit codes of the index, rebuild and verify CLI: 0 success, 1 errors or drift, 2 usage, 3 another vector run in progress. `make dev` starts Postgres and Qdrant and migrates.

## Limits

Chunk-level points are supported by the contract (`chunk_id`) but the indexer writes page-level points only (`point_from_embedding` sets no chunk). A single collection holds one vector size and one embedding identity at a time.
