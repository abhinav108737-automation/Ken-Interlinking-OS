# Phase 2 Report — Content Intelligence

**Status: complete.** Executed under the user's 2026-10-14 authorisation for autonomous sequential
delivery of Phases 2–9. Each phase has internal gates, a local commit and a `phase-N-complete` tag.
Nothing has been pushed.

**Design:** [docs/adr/ADR-0010-content-intelligence.md](docs/adr/ADR-0010-content-intelligence.md),
which also amends ADR-0003.

## 1. What was built

Every page's **current version** now has a machine-usable understanding, derived deterministically
from a versioned taxonomy policy, plus a local embedding:

| Capability | Implementation |
|---|---|
| **Taxonomy** | `policies/taxonomy/taxonomy.v1.yaml`: 29 markets in an industry tree, 23 geographies, a 13-entry fictional gazetteer, 6 page-type rules, protection rules, eligibility thresholds. Validated on load: unique ids, references resolve, no parent cycles. An illustrative seed, **not** Ken's taxonomy. |
| **Analysis** (no LLM) | page type, intent, commercial importance, industry and market (highest-scoring confident node), topics, geographies, entities, keywords, extractive summary, salient blocks. Immutable per (page version, `analysis_version = tax1-alg1`). |
| **Embeddings** | `ModelProvider.embed(role=EMBEDDING)`. Local default **`nomic-embed-text`** (Apache-2.0, 137 M parameters) via Ollama. The dimension is read from the provider at runtime and the column is dimension-less. Identity = the provider-reported model plus dimensions, recipe version and recipe fingerprint. The mock provider is deterministic, lexically meaningful feature hashing. |
| **Semantic similarity** | exact cosine KNN in pgvector over current versions, filtered by identity |
| **Relationships** | typed, weighted, evidenced, version-stamped edges: same, parent, sub-, related, supply-chain and complementary market, plus `semantic_similar` top-k. Coarse types (shared geography, regional parent, industry adjacency) are computed on demand, not stored. Stored types are capped per (source, type). |
| **Protections** | policy rules matched on a canonical path; the CMS's own flag, mirrored by ingestion on every document (only a live CMS read may clear it); manual (Phase 5). Deactivated, never deleted. |
| **Link eligibility** | computed at **read time** from live state, never stored. It fails closed on: not published, gone, protected, noindex, alias, non-200, thin content, excluded page type, missing analysis and stale analysis. |
| **Job runs** | generic `job_runs` for every batch job from Phase 2 on. Per-kind advisory lock, safe failure reasons, new governed event `job.completed`. |
| **API** (GET-only) | `/intelligence/coverage`, `/taxonomy`, `/taxonomy/{node}/pages`, `/pages/{id}` (+ `/similar`, `/relationships`), `/search?q=` (503 `model_unavailable` when the model is down), `/jobs/runs`, `/jobs/runs/{id}` |
| **UI** | `/intelligence` (coverage + taxonomy tree), `/intelligence/topics/[id]`, `/intelligence/search`, `/jobs`, `/jobs/[id]`, and an Intelligence panel on every page detail |
| **CLI** | `make analyze` (`ARGS="--force"` / `"--no-embeddings"`); idempotent, only stale pages are processed |

## 2. Schema — migration `0003_content_intelligence`

- **Extension:** `CREATE EXTENSION vector`, which ADR-0003 deferred to this migration.
- **Tables:**
  - `job_runs`;
  - `page_analyses` (immutable; GIN indexes on topics, geographies and entities);
  - `page_embeddings` (immutable; dimension-less `vector`; CHECK `vector_dims(embedding) = dimensions`;
    unique per page version, model, recipe version and recipe fingerprint);
  - `page_relationships` (both endpoint versions; no foreign keys reference it);
  - `page_protections` (partial unique index on active rows).
- **Downgrade:** restores the exact 0002 state, including dropping the extension. QA verified this
  byte-for-byte with `pg_dump`.
- **pgvector is now required.** A server without it can apply up to 0002, and the build then
  reports `/ready` 503. CI asserts that 0003 fails *because* the extension is missing.

## 3. Results on the synthetic demo corpus (65 pages, live `nomic-embed-text`)

| Measure | Value |
|---|---|
| Analysis goldens (spec-derived, 65 pages) | **all match** |
| Retrieval (42 queries, 98 labelled pairs): recall@10, taxonomy / nomic embedding / combined | 0.988 / 0.984 / 0.988 |
| Retrieval MRR, nomic embedding / combined | 0.968 / 0.948 |
| Pairwise strong-over-unrelated, combined | **1.000** |
| Live analysis run (64 pages, embed + relate) | 6 s, 0 errors; re-run is a no-op |
| Stored taxonomy edges after the QA fix | 514 (was 3,531), max out-degree 19 |
| Similar pages, "battery swapping for e-2W in India" | India e-2W (0.87), India EV charging (0.84), Indonesia e-2W (0.82) |
| Semantic search, "cold storage for vaccines in the gulf" | UAE cold chain first |

The full benchmark is in `evals/intelligence/README.md` and `results/*.json`.

**Caveat:** the corpus and taxonomy were written together, so absolute numbers overstate quality
on the real site.

## 4. Dependencies

**None added.**
- The embedding model `nomic-embed-text` (Apache-2.0) was pulled into the local Ollama.
- pgvector comes from the existing `pgvector/pgvector:pg16` image.
- The `pgvector` Python package is not needed: asyncpg exchanges the type as text.

## 5. Verification (final run, on the final code)

| Gate | Result |
|---|---|
| `make verify` | **exit 0**: ruff clean, mypy clean (163 files), **1,672** Python tests passed, **624** web tests passed, compose ×3 OK, doctor **0 FAIL / 0 WARN** |
| Live Ollama tests | 5 passed: dimensions read at runtime (768); related texts score above unrelated |
| Alembic round trip | head → 0002 → head, on the test DB, the QA DB and the dev DB rebuild |
| OpenAPI drift | live == committed `openapi.json`; `types.gen.ts` regenerated |
| Electron guard | lint clean; no Electron import; no raw-HTML rendering |
| Live E2E | demo corpus → `make analyze` (real model) → all 9 new routes 200 (404/503 paths correct) → all new screens 200 |

## 6. Reviews

**Architecture review (pre-fan-out): APPROVED WITH CHANGES.** All 7 changes were made before
implementation:
- truthful degraded-mode wording;
- the CI pgvector check;
- provider-reported embedding identity;
- relationship endpoint versions and replacement in both directions;
- an entities index;
- CONTRACTS updates;
- the TRD §5 mapping.

**Architecture review (final): APPROVED WITH CHANGES.** R1–R5 were all done:
- only a live CMS read clears a CMS protection;
- a single CMS-protection mechanism;
- the ADR revision note;
- static, typed imports;
- an empty `dburl` is refused.

**QA/security: BLOCKERS FOUND, then all fixed and re-verified by the same reviewer: may close.**
The 1k-page scale probe after the fix gave:
- 82,247 edges, down from 988,400;
- 28 MB, down from 347 MB;
- 42 s, down from 197 s;
- 122 MB RSS, down from 640 MB;
- incremental == forced, byte for byte.

The re-review raised three small follow-ups, all fixed:
- semantic edges now record the (model, recipe fingerprint) that produced them; edges from another
  identity are recomputed and never served;
- repeated slashes (`//privacy`) are collapsed before protection matching;
- a docstring order fix.

Migration 0003 was **edited in place before any deployment**, to add the recipe fingerprint. The
project dev and test databases were rebuilt to it (0003 → 0002 → head), which is permitted for
project databases. No other database ever ran 0003.

| Ref | Finding | Resolution |
|---|---|---|
| B1 | An empty `-x dburl` fell back to the dev DB | Refused, with a test |
| B2 | Quadratic taxonomy edges (≈1 M rows, 347 MB, 197 s for 1k pages) | Coarse types derived, not stored; per-(source, type) cap; incremental == forced (tested) |
| H1 | A file export could clear a CMS protection | Asymmetric sync; policy rule removed |
| H2 | Protection paths failed open on `/Privacy`, `/privacy;jsessionid=1`, `.html`, locales | Canonical-path matching and a broader pattern, 14 variant tests |
| M1 | Recipe settings were not part of embedding identity | Recipe fingerprint in identity; vectors never mixed |
| L | Fictional company name matched a real company | Renamed "Zorvex Pay" |

M2–M5 and the remaining low findings are recorded in NEXT_STEPS for later phases:
- model digest in identity;
- an identity cache;
- SQL coverage aggregation;
- a `page_protections` trigger;
- a job reaper.

## 7. Incidents (disclosed)

**Phase 1 dev-DB reset, found in Phase 2.** During the Phase 1 final checks, I ran a migration
round trip in a shell loop that passed `-x dburl=…` as one argv word.
- Alembic read the key as `" dburl"`, did not recognise it, and fell back to `KEN_DATABASE_URL`:
  the project **dev** database on 5433.
- That downgraded and re-upgraded the dev DB, erasing its Phase 1 fixture data, which was
  reproducible.
- The user's PostgreSQL on 5432 was never involved, and the 5432 guard held.
- **Fix:** strict `-x` parsing. Keys are stripped, and unknown or empty keys are refused rather than
  falling back. Regression tests cover both argv forms.
- During the QA review, one read-only `alembic current` probe also reached the dev DB through the
  then-unfixed empty-value path. Nothing was written.

## 8. Known gaps

- **Taxonomy:** v1 is a fixture seed. Ken's real taxonomy is needed (NEXT_STEPS #7).
- **Similarity scale:** exact KNN is O(pages); an ANN index waits for measured latency.
- **Similarity threshold:** a single global, model-specific value; re-calibrate when the model
  changes.
- **Embedding identity:** it does not include the model digest.
- **Reader latency:** the reader asks the provider for its identity on each request; it needs a TTL
  cache.
- **Coverage:** aggregation is O(pages) in Python.
- **PRD wording:** PRD v1.1 §17.1's "genuine native degraded mode" now holds only for schema ≤ 0002.
  This needs your approval for the next PRD revision.

## 9. Information still needed from you

These are unchanged from Phase 1, plus:
- **Ken's real taxonomy:** markets/aliases, geographies, gazetteer, URL page-type patterns and
  protected sections.
- **Approval of the PRD §17.1 wording revision.**

None of these blocks Phase 3.
