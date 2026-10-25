# Local Shadow Pilot Report

Date: 2026-10-25 · Mode: `shadow` (nothing published) · Base commit: `c4f6404`

## 0. Read this first

* **The corpus is synthetic.** All 64 pages are `https://research.example.com/...`, imported from the
  repository fixture `tests/fixtures/demo/research_site.jsonl` by `import-file` (two earlier runs on
  2026-10-14 in `ken_interlink`; a third in the fresh `ken_pilot` database). `example.com` is a reserved
  placeholder domain. **Nothing here is real Ken Research content**, so no statement below says anything
  about link quality on Ken's real pages.
* **Nothing was published.** `deployments`, `deployment_items` and `publish_attempts` are empty in both
  databases; the CMS provider was `mock`, the autonomy mode `shadow`. No real website was crawled; no
  production credentials were requested.
* Scores, critic verdicts and decisions are real outputs of the real pipeline and a real local model
  (`qwen3:8b-q4_K_M`, `nomic-embed-text` via Ollama), but they are **uncalibrated** (no review history exists:
  `calibration_version` is empty on every decision).

## 1. Runtime configuration (confirmed)

| Item | Value |
|---|---|
| Application role | `ken_app` (no superuser, no DDL/TRUNCATE, owns nothing); `make doctor` role check OK |
| Migration role | owner `ken`, via `KEN_MIGRATION_DATABASE_URL` (`.env`, mode 0600) |
| Vector store | Qdrant v1.19.1, `KEN_VECTOR_STORE=qdrant`, collection `ken_pages` (dev) / `ken_pilot_pages` (pilot) |
| Embedding model | `nomic-embed-text` (Ollama, 768 dimensions, Apache-2.0), cosine, recipe `r1-9e0f94f9f2f45746` |
| Reasoning model | `qwen3:8b-q4_K_M` (Ollama; Apache-2.0 weights) |
| Paid APIs | none |

## 2. Corpus and indexing

* 64 pages (+1 duplicate row skipped), 67 stored embeddings in `ken_interlink` (3 older page versions).
* Indexing is idempotent and keyed by content: first `make qdrant-index` upserted 64 points; the immediate
  re-run upserted **0**. Each point carries `page_id`, `page_version_id`, `content_hash`, `embedding_model`,
  `embedding_version` (recipe fingerprint) and `page_type`/`language`; point ids are deterministic `uuid5`.
* `make qdrant-verify`: in sync, 0 missing / orphaned / stale, status green, on both `ken_pages` and the
  independent pilot collection.

### Retrieval and drift tests (scratch collection, then deleted)

| Test | Result |
|---|---|
| Semantic search, 4 queries, real query embeddings | Sensible neighbours, e.g. "electric vehicle market size in Asia" -> Thailand/Indonesia/India EV pages (0.80-0.77); "careers and jobs" -> `/careers` (0.71) then a steep drop (0.55). Sample: `docs/pilot/semantic_search_sample.json` |
| Top-K | `limit=3` is exactly the prefix of `limit=5`; `similar_to_page` excludes the page itself |
| Stale-vector detection (read time, decided in PostgreSQL) | real hit not stale; tampered content hash, superseded version id and unknown page all flagged stale |
| Stale / orphan detection (index drift) | one tampered `content_hash` -> `stale_points=1`; one point for a page absent from PostgreSQL -> `orphaned_points=1`; one deleted point -> `missing_vectors=1` |
| Rebuild | `rebuild` dropped and refilled 64 points; verify back to in sync |

My first hand-made "orphan" had an incomplete payload and was (correctly) not parsed as a reference; the
test was redone with a full payload and then detected.

## 3. Shadow pipeline (fresh database `ken_pilot`, current code path)

import-file -> analyze (64 pages analysed, 64 embeddings, 805 relationships, 2 protections) -> qdrant-index ->
recommend -> agents (critic + policy gate) -> autonomy (shadow).

* **Recommend:** 1,773 candidates -> 80 opportunities -> 48 plans. Main rejections: 1,573 no natural anchor,
  37 target excluded, 32 target ineligible, 29 already linked, 27 target protected, 24 below minimum relevance.
* **Critic (live qwen3:8b):** 35 accept, 10 reject, 3 uncertain (2 deterministic fallbacks after
  `model_unavailable`, never treated as accept; 1 model "uncertain"). Accepted plans moved
  `proposed -> critiqued -> policy_checked` (35); the rest stayed `proposed`.
* **Autonomy (shadow, 48 plans):** 24 would be `AUTO_PUBLISH_WITH_EXTRA_VERIFICATION`; 21 would be `BLOCK`
  (10 critic rejects, 11 accepted plans whose confidence 0.38-0.49 is below the 0.50 autonomous floor); 3
  uncertain plans were declined with no decision row. **0 hard block codes fired.** 0 plain `AUTO_PUBLISH`
  (needs a calibrated class, impossible without review history). Every recorded decision has
  `decision=block` because shadow records but never acts; `would_have_decision` is the answer.
  The cleanest separation: all deferred accepts <= 0.488, all extra-verification >= 0.522.
* Confidence equals the raw plan score here (uncalibrated), range 0.38-0.76.
* **Runtime (corrected 2026-10-25):** the agent step's wall clock was about 2 hours, but that was **host sleep, not
  the critic**. The run was not under `caffeinate`; `pmset -g log` shows the Mac asleep from 00:49 IST with 45-second
  DarkWakes about every 15 minutes, and 8 of the 30 agent runs each span exactly one such gap (910-977 s) while their
  measured step times are 8-13 s. Active compute was about 6 minutes (see `docs/pilot/SHADOW_PILOT_PERFORMANCE_REPORT.md`).

### Audit export

`docs/pilot/shadow_pilot_recommendations.{json,csv}`: all 48 plans with source, destination, anchor text,
block id and character offsets, before/after context, the full scoring breakdown (semantic, taxonomy,
intent, graph, geography, commercial, underlinked, historical: value, weight, contribution), reason codes,
channels, critic verdict/reasons/fallback, plan status, autonomy mode, decision, `would_have_decision`, risk
tier, confidence and blocks. The JSON header records the table counts proving no CMS writes.

## 4. Findings and limitations

1. **Legacy plans in the dev database are stranded.** `ken_interlink` holds 33 plans in the pre-ADR-0018
   status `awaiting_approval` (written 2026-10-14). The current agent flow ends at `policy_checked`, and the
   autonomy engine's decidable set excludes `awaiting_approval`, so a shadow pass on the dev DB saw only the
   15 critic-rejected plans (15 shadow BLOCK decisions recorded there). This is why the full flow was run in a
   fresh database. Nothing was edited in the dev database's plan states. Recommend rebuilding the dev DB.
2. The pipeline is deterministic where it matters, but the critic is a small local model: its accept/reject
   quality on real content is **unmeasured**. The test corpus is also tiny and templated.
3. 1,573 of 1,773 candidates were rejected for "no natural anchor": expected for short synthetic pages, not
   evidence about real pages.
4. `DeploymentView` tags and verification were not exercised (nothing was published).
5. (Corrected) The critic is not 2 hours slow: it is about 10 s per source. Long local jobs must run under
   `caffeinate -i` (now built into `make agents`).
6. `ken_pilot` (database) and `ken_pilot_pages` (collection) were left in place for inspection; drop them
   with `DROP DATABASE ken_pilot` and a `DELETE /collections/ken_pilot_pages` on the project Qdrant.

## 5. Exact requirements for a real Ken Research pilot

1. **Approval of the real source (ASK):** the exact base URL and allowed hosts to crawl, or a JSONL/CMS
   export. No URL was guessed. The crawler now refuses private addresses; crawling is read-only and robots-
   respecting (`KEN_CRAWL_BASE_URL`, `KEN_CRAWL_ALLOWED_HOSTS`).
2. **A representative sample** (suggest 200-500 pages across report, insight, industry-hub and geography page
   types) plus the real CMS body/HTML templates so block hashes and anchor placement can be validated
   (`make cms-sim-seed --export` parity check, NEXT_STEPS row 14).
3. **Human relevance judgements:** have Ken editors label a few hundred shadow recommendations
   (accept / reject / wrong anchor). This is the only way to learn real precision, tune the 0.50/0.85
   thresholds, and start calibration. Until then every plan is uncalibrated.
4. **Model sizing:** at about 10 s per source the critic needs roughly 3 hours for 1,000 sources on this Mac;
   measure again on real pages (see the performance report).
5. **Keep `shadow`**; no CMS credentials are needed for a read-only shadow pilot. Real CMS write access,
   `KEN_AUTONOMY_ALLOW_PRODUCTION`, a named operator account and consent for any beacon are separate ASK items
   (NEXT_STEPS rows 12, 24, 25).
6. **Clean database:** run the pilot in a fresh database (finding 1) with `make db-roles` applied, Qdrant up,
   and `make qdrant-verify` green before reading any recommendation.

## 6. Reproduce

`make dev` -> `make analyze` -> `make qdrant-index` -> `make qdrant-verify` -> `make recommend` ->
`make agents` (long) -> `make autonomy-shadow`; the export is a read-only join of `link_plans`, `agent_run_plans` and `autonomy_decisions` (output in `docs/pilot/`).
