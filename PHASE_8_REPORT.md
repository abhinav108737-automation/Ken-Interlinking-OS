# Phase 8 Report — Learning

**Status:** gate passed on 2026-10-29. Design: [ADR-0016](docs/adr/ADR-0016-learning.md). Real-site bootstrap
done in the same session: [ADR-0025](docs/adr/ADR-0025-real-site-bootstrap.md).

**Read this first.** Phase 8 was implemented in earlier sessions (commits `295b94b` … `ff08b6c`), and
Phase 9 work was committed ahead of this gate. This report records the **verification** of that work and
the first run on **real** content. It does not describe a fresh build, and the learning code was not
re-reviewed line by line in this session.

## 1. What exists

| Area | Where | Size |
|---|---|---|
| Contracts | `ken_contracts/learning.py` | frozen (W0) |
| Schema | migration `0009_learning` (proposals, transitions, candidates, skills, evaluations, experiments) | — |
| Services | `services/ken_services/learning/` — reflect, proposals, datasets, scorers, evaluate (offline runner), shadow, promote, lifecycle, experiments, aggregate, reader, metrics, dedupe, CLI | 19 modules, about 9,600 lines |
| Tests | `tests/unit/learning/`, `tests/contract/test_learning_contracts.py`, `tests/integration/test_learning_*.py`, `tests/unit/test_api_learning.py` | see §3 |

The pipeline is the one CLAUDE.md requires: experience → structured feedback → proposal → offline eval →
shadow/canary → **explicit human promotion**. Nothing self-promotes.

## 2. Safety defaults (unchanged, by design)

- `KEN_LEARNING_DEMONSTRATION_MODE` defaults `true` and **refuses every promotion**. Phase 8 therefore ends
  with zero promotions by construction. Turning it off is your decision (NEXT_STEPS item 20).
- `KEN_EXPERIMENTS_ALLOW_HOLDOUT` defaults `false`; a holdout withholds links from real pages (item 21).
- Hot reload of a promoted policy is not built; a promotion needs an operator restart (item 19).

## 3. Verification (final code, 2026-10-29)

| Check | Result |
|---|---|
| `ruff check` (services, contracts, evals, agents, tests) | clean |
| `mypy` | clean, 508 source files |
| Learning unit + contract tests | 863 passed |
| Learning integration tests (real Postgres, `ken_interlink_test`) | 174 passed |
| **Full suite** (excluding `tests/e2e`) | 6,187 passed, 37 skipped, 3 failed → see §4; after the fix, the 3 pass |

The 37 skips are platform-specific: POSIX file modes (0600 token files), asyncio signal handlers and
`make` (not installed here). They are Windows limits, not hidden failures.

## 4. Incidents

1. **178 integration tests silently skipped** at first ("test database unreachable"). The default test URL
   says `localhost`, which does not connect on this Windows machine; `KEN_TEST_DATABASE_URL` with
   `127.0.0.1` fixes it. The default in the repository was **not** changed. Anyone running tests on
   Windows should set it, or the integration gate will pass vacuously.
2. **Three failures in the full run.**
   - `test_learning_experiments::…recommendation_pipeline` and `test_migrations::…round_trip` passed when
     rerun alone. They ran while this session was also analysing and recommending against Postgres, so
     treat them as **load-sensitive**, not as fixed. They passed again in the final run.
   - `test_analytics_aggregate_seed::…hash_seed` was a real, platform-specific test defect: it starts a
     subprocess with an almost empty environment, and Windows cannot start asyncio without `SYSTEMROOT`.
     The test now passes that variable through when it exists. No product code changed.

## 5. First run on real content (not a gate; an observation)

Source: 498 pages of kenresearch.com (a 6 % stratified sample), database `ken_real500`, **proposed**
taxonomy v2.

| Step | Result |
|---|---|
| Analysis + embeddings (`nomic-embed-text`) | 497 pages, 497 embeddings, 6,746 relationships, 0 errors |
| Deterministic recommender | 28,484 candidates → **126 plans** in 160 s |
| Main rejection reasons | `page_type_incompatible` 17,283; `no_natural_anchor` 10,604; `target_ineligible` 245 |
| Local critic, `qwen3:8b-q4_K_M`, 10 sources | 27 plans reviewed: **6 handed to the human queue, 21 rejected**; 0 errors, 0 fallbacks; 327 s |

**What a person should look at.** The handed-off plans include same-topic links across countries on a
single generic word: "cosmetics" (an Indonesia report linking to an India report), "butter" (a North America
nut-and-seed report linking to a USA butter report, probably from "peanut butter"). That is NEXT_STEPS item 9
and exactly what the structured feedback reason `anchor_too_generic` exists to capture. It is a sample of 6,
not a measurement; it argues for reviewing the queue before trusting the critic.

**No learning signal exists yet.** The corpus has no human review actions, so reflection has nothing real to
learn from. The earlier synthetic corpus (about 65 pages, scripted reviews) is plumbing, not evidence.

## 6. Known gaps

- Real editorial volume is the gate on learning being more than plumbing (item 20).
- `review_performance` (ADR-0012 §42) is carried to Phase 9 (item 23).
- The Search Console query import is deferred; search-side learning has no data (item 22).
- Google access is enabled (`KEN_ANALYTICS_GOOGLE_ENABLED=true`) but **not yet exercised** against the live APIs.
- The full-suite integration tests are load-sensitive when other work shares the database container.
- The sample is 6 % of the site; the proposed taxonomy tags markets mostly on reports and benchmarks.

## 7. Information still needed from you

The decisions are listed in NEXT_STEPS.md; the ones that gate the next step are items 7 (review
`taxonomy.v2.yaml` and supply a company/product list), 9 (generic-anchor policy), 12 (CMS API contract and
credentials) and 20 (when learning may promote).
