# Recommendation evals (Phase 3)

These evals cover the deterministic recommendation engine of
[ADR-0011](../../docs/adr/ADR-0011-recommendation-engine.md):
- retrieval channels;
- the filters (§3);
- the transparent score (§4);
- natural placement (§5);
- the freshness check (§6).

They run over the synthetic demo corpus in
[`tests/fixtures/demo/`](../../tests/fixtures/demo/README.md).

**No learning behaviour.** These are offline evals (`RUN_OFFLINE_EVAL`, ALLOW). They read
fixtures and write a report. Ablations build policy **copies in memory**. Nothing here writes,
proposes or promotes a policy, skill or setting.

## Datasets

| File | What it is |
|---|---|
| `tests/fixtures/demo/recommendation_judgments.json` | Spec-derived judgments for **all 46 eligible sources**. Holds 75 GOOD targets, each with the unlinked mention expected as its anchor, the relationship and why. Also 158 ACCEPTABLE and 928 BAD targets (BAD includes the rule-expanded R1–R4 entries). Also: the 19 **must-not** rules; **24 expected anchors**, pairs whose source has exactly one natural candidate phrase; and one synthetic **placement-safety page**. |
| `tests/fixtures/demo/stale_context_cases.json` | 13 `check_plan_freshness` cases: fresh, rebasable (identical body, block moved down or up, another block edited, second occurrence) and stale (context changed, block missing, block deleted with its id reused, anchor moved, anchor removed, anchor wrapped in a link, paragraph moved into a table). |

### How the judgments were written

The judgments were written from the ADR, `taxonomy.v1`, the Phase 2 `relatedness.json` rubric and
the corpus text, **before** the engine existed. Engine output was never pasted in.

- **GOOD** needs both:
  - a strong relation under the relatedness rubric;
  - an unlinked natural mention in an eligible block. Under policy v1 that is a `p` or `li` with
    no existing link, because `max_links_per_block = 1`.
- **ACCEPTABLE** covers:
  - weak relations;
  - cross-geography links on the same topic;
  - strong relations that have no free mention.
- **BAD** covers geography-only overlap, shared-word false friends, intent mismatches (listing hubs,
  the market-less digest, other industries' hubs), unrelated markets and ineligible pages.
- Rules R1–R4 in the file expand a few classes mechanically:
  - R1: relatedness `unrelated` pairs are BAD;
  - R2: listing hubs and the market-less digest are BAD;
  - R3: an industry hub is ACCEPTABLE for its own industry and BAD otherwise;
  - R4: pages that are not linkable targets are BAD.

**Two pairs were added after an engine run surfaced them as unjudged.** They were judged by the
same rubric and are flagged in their `why`:
- Bangladesh pharma → UAE cold chain;
- Indonesia EV charging → battery-swapping insight.

`tests/unit/recommendation_evals/test_judgments_dataset.py` checks the judgments against the
corpus:
- every GOOD mention occurs in a free block;
- no GOOD or ACCEPTABLE target is already linked or ineligible;
- the labels agree with `relatedness.json`;
- every expected anchor is the **only** market phrase of its target in the source's free blocks;
- the must-not page lists equal the eligibility goldens.

## What is measured

| Eval | File | Kind |
|---|---|---|
| Must-not rules (19) | `tests/integration/test_recommendation_evals.py` | hard gate, zero tolerance, over every plan |
| Quality metrics | same | regression floors |
| Idempotency and locator round-trip | same | pass/fail |
| Placement safety (excluded markup) | same, second test | pass/fail |
| Freshness fixtures | `tests/unit/recommendation_evals/test_stale_context.py` | pass/fail |
| Harness self-tests | `tests/unit/recommendation_evals/test_eval_harness.py` | every gate must fire on a bad record |
| Ablation benchmark | `evals/recommendations/benchmark.py` | report only |

**Metrics.** Plans of one run, against the judgments:
- **precision@3:** per source with plans, |plans in GOOD ∪ ACCEPTABLE| / |plans|, then the macro
  mean. Unjudged targets count as not relevant.
- **recall of GOOD:** |plans in GOOD| / min(|GOOD|, `max_plans_per_source` = 3), then the macro
  mean.
- **BAD rate:** the share of judged plans whose target is BAD.
- **anchor exact match:** a verbatim, case-sensitive match on the expected-anchor pairs the engine
  proposed.
- **anchor-names-target-market rate (informational):** the share of plans whose anchor is a
  label or alias of the target's market.

**Must-not rules.** These are checked independently of the engine, from database rows, the
corpus and the policy. The plans checked are those of `run_recommendations` over the demo corpus,
and again with the safety page appended. A plan must never:
- have a protected, draft, thin, alias, gone or non-linkable source or target;
- link to itself;
- link to a target that the current version already links to (through `known_urls`).

Its anchor must never:
- be a geography alias on its own;
- be a forbidden anchor;
- sit in a block of a non-`p`/`li` kind;
- sit under an excluded ancestor. The block's `enclosing` and the span's run are checked, and the
  safety page proves it with tables, captions, blockquotes, figures, `pre`/`code`, `cite`, `q`,
  `sup` and `sub`;
- overlap an existing link, or push a block over `max_links_per_block`.

The verbatim slice, the context hash and the occurrence index must hold. The run must respect:
- the per-source, per-target and link-density caps;
- uniqueness of open pairs and spans;
- the score thresholds and identities;
- raw confidence: `calibration_version` is null and `confidence` equals `total`.

## How to run

```bash
KEN_REQUIRE_INTEGRATION_DB=1 .venv/bin/pytest -q tests/integration/test_recommendation_evals.py -rA
.venv/bin/pytest -q tests/unit/recommendation_evals
.venv/bin/python evals/recommendations/benchmark.py                    # -> results/mock.json
.venv/bin/python evals/recommendations/benchmark.py --provider ollama  # -> results/ollama-nomic-embed-text.json
```

The integration test and the benchmark use **only** the `*_test` database. The benchmark refuses
any other database name, because it migrates and truncates tables. Do not run it while other
integration tests are using the same database: ingestion takes an advisory lock and truncation
races.

Ollama mode reads no `.env` and talks only to `127.0.0.1:11434`. Mock mode sets the semantic
floor to 0.3, because feature-hashing cosines are low. Ollama mode keeps the calibrated 0.78.

## Results (2026-10-14, Apple Silicon, 46 judged sources, final Phase 3 engine)

| Config | Provider | Plans | Sources with plans | P@3 | Recall GOOD | BAD rate | Anchor exact | Must-not |
|---|---|---|---|---|---|---|---|---|
| all | mock | 46 | 27 | 0.98 | 0.20 | 0.022 | 1.00 (11/24) | 0 |
| only_taxonomy | mock | 39 | — | 1.00 | 0.18 | 0.000 | 1.00 | 0 |
| only_graph | mock | 31 | — | 1.00 | 0.15 | 0.000 | 1.00 | 0 |
| no_* (any one channel off) / graph_weight_0 | mock | 45–46 | 27 | 0.98 | 0.20 | ≤ 0.022 | 1.00 | 0 |
| all | ollama `nomic-embed-text` | 46 | 27 | 1.00 | 0.21 | 0.000 | 1.00 (13/24) | 0 |

**The anchor rule behind these numbers.** This is a reviewed decision; see ADR-0011 §5 and
`FLOORS` in the integration test.
- **Before the rule (107 plans, recall 0.94):** most plans were same-market, cross-country links
  anchored on the source page's *own* market phrase, such as "cold chain" on the India report
  linking to the UAE report. That anchor describes the page the reader is already on, which is
  misleading (PRD v1.1 §12.4).
- **The rule:** such a phrase is a valid anchor only when the same block names one of the
  target's distinctive geographies, such as "electric vehicles … in Thailand". The engine
  dropped about 60 plans, and recall of the GOOD judgments fell to 0.20. Every safety gate
  stayed green, and precision rose or held.
- **The dropped relationships are still visible:** `same_market` edges on every page's
  Intelligence panel.
- **Recovering them needs natural text.** The source must name the target's country or region
  next to the market phrase. Content editors can add that, or Ken's real pages may already
  contain it.

**Determinism:**
- With the mock provider, two independent ingestions give identical plans
  (`determinism_after_reingest.identical = true`).
- With nomic, 1 of 46 plans differed between ingestions. Local embedding inference has small
  floating-point noise, which flips a near-tie in the k-nearest-neighbour search. That is model
  nondeterminism, not engine order. Sources are processed by canonical URL.

**Ablations:** each channel alone keeps precision. On this 46-target corpus, any single channel
off changes almost nothing, because k = 30 is nearly exhaustive. The `graph` complementarity
component shows no measurable value here (weight 0 changes one plan). Re-weighting it is a
policy v2 decision, which needs a human.

## Regression floors (integration test)

| Metric | Floor | Measured (mock) |
|---|---|---|
| precision@3 (macro) | ≥ 0.90 | 0.98 |
| recall of GOOD (macro) | ≥ 0.15 (lowered from 0.80, a reviewed decision; see Results) | 0.20 |
| anchor exact match | ≥ 0.85, with ≥ 5 pairs proposed | 1.00 (11 pairs) |
| BAD rate | ≤ 0.03 | 0.022 (nomic 0.00) |
| unjudged rate | ≤ 0.05 | 0.00 |
| sources with plans | ≥ 25 of 46 | 27 |

Floors sit a little below the measured values. They are a regression gate, not a target, and
lowering one is a reviewed decision. The recall floor was lowered once, for the self-describing
anchor rule; the reason is in Results above and in `FLOORS` in the test. Every must-not rule is
zero-tolerance.

## Caveats

- **Synthetic corpus, fixture taxonomy.**
  - The 65 pages and `taxonomy.v1` (`source: fixture`) were written together.
  - Topic aliases appear in the text by construction, and placement matches those same aliases.
  - Absolute numbers therefore overstate real-site quality. Rerun on Ken's taxonomy and content.
- **The judgments favour the taxonomy.** GOOD is defined through markets, geographies and declared
  relations, which are the concepts the engine scores. Read the metrics as consistency and
  regression checks, not as editorial quality.
- **Raw confidence.** `confidence` is the raw `total` (`calibration_version = null`). No metric here
  is a calibration measurement, and no threshold may treat these numbers as calibrated (Phase 8).
- **Small sample.** With 46 sources and about 105 plans, one plan moves precision by about 0.01
  and recall by about 0.02.
- **The freshness reason `anchor_moved`.** The engine reports `context_changed` when the anchor
  moved or vanished inside an edited block. The fixture expects `anchor_moved`, and these cases
  are strict xfails. The verdict is `stale` either way, so the behaviour is safe; only the
  diagnostic differs.
