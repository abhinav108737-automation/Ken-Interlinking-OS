# Content-intelligence evals (Phase 2)

These evals cover the deterministic analyser, protection, eligibility and the retrieval channels
of ADR-0010. They run over the synthetic demo corpus in
[`tests/fixtures/demo/`](../../tests/fixtures/demo/README.md).

**No learning behaviour.** Nothing here proposes, promotes or changes a policy, skill or setting.
These are offline evals (`RUN_OFFLINE_EVAL`, ALLOW): they read fixtures and write a report.

## What is measured

| Eval | File | Kind |
|---|---|---|
| Analysis goldens | `tests/unit/intelligence_evals/test_analysis_goldens.py` | pass/fail, spec-derived |
| Retrieval metrics | `tests/unit/intelligence_evals/test_retrieval_eval.py` | metrics with regression floors |
| Embedding benchmark | `evals/intelligence/benchmark.py` | report only (mock or local Ollama) |

**Analysis goldens.** All 65 pages are checked against `analysis_expected.json`. Each page is read
with `FileExportContentSource`, normalised by the Phase 1 `normalize_document`, then fed to
`analyze_page` and checked for:
- page type, intent and commercial importance;
- `market_id` and `industry_id`;
- the expected geographies and entities (at least those listed).

Two more checks run on every page:
- `match_protections`: the exact set of rules;
- `compute_eligibility`: the exact source and target reason sets. The `EligibilityState` is built
  from the normalised record, with a fresh analysis and `gone=False`.

**Corpus invariants.** The committed JSONL equals the generator's output, it follows the export
format, it contains every required special case and links only to reserved domains, and the
retrieval pool equals the set of eligible reports and insights.

**ADR properties.** The `tax1-alg1` analysis version, determinism, extractive summaries capped at
600 characters, and the ADR-0010 §3 market rule.

**Retrieval.** Every page in the pool that has at least one `strong` partner is a query: 42 queries
over a pool of 46 pages. The candidates are all the other pool pages, and there are three channels:
- **taxonomy:** the sum of `taxonomy_relationships(query, candidate)` weights;
- **embedding:** the cosine of the page embeddings. The text comes from `embedding_text(...)`,
  using the `KEN_EMBEDDING_*` defaults: a 6000-character cap and the `search_document: ` prefix.
- **combined:** taxonomy and embedding, each min-max normalised per query, then summed.

The metrics:
- recall@5 and recall@10 over `strong` partners (macro average);
- MRR of the first `strong` partner;
- mean rank of strong, weak and unrelated partners;
- **pairwise**: the share of (query, strong, unrelated) triples where the strong partner ranks
  higher.

Ties are broken by URL, so every run is reproducible.

## How to run

```bash
.venv/bin/pytest -q tests/unit/intelligence_evals -rA          # goldens + retrieval floors (mock)
.venv/bin/python evals/intelligence/benchmark.py --provider mock
.venv/bin/python evals/intelligence/benchmark.py --provider ollama   # needs: ollama pull nomic-embed-text
.venv/bin/python evals/intelligence/benchmark.py --provider ollama \
    --output evals/intelligence/results/ollama-nomic-embed-text.json
```

The pytest suite needs no database, network or model server.

Ollama mode uses `build_model_provider(Settings(model_provider="ollama"))` and reads no `.env`. Its
only network target is the local daemon at `127.0.0.1:11434`, and it refuses any other host. It is
opt-in and never part of CI.

## Results (2026-10-14, Apple Silicon, 46-page pool, 42 queries)

Saved reports: [`results/mock.json`](results/mock.json) and
[`results/ollama-nomic-embed-text.json`](results/ollama-nomic-embed-text.json).

| Channel | Provider | R@5 | R@10 | MRR | Mean rank (strong / weak / unrelated) | Pairwise |
|---|---|---|---|---|---|---|
| taxonomy | — | 0.915 | 0.988 | 0.893 | 2.9 / 5.9 / 14.9 | 0.985 |
| embedding | mock (feature hashing, 768-d) | 0.788 | 0.932 | 0.944 | 4.2 / 10.2 / 16.1 | 0.892 |
| combined | mock | 0.959 | 0.996 | 0.946 | 2.4 / 5.2 / 14.3 | 0.992 |
| embedding | ollama `nomic-embed-text` (768-d) | 0.928 | 0.984 | 0.968 | 2.6 / 6.7 / 9.9 | 0.962 |
| combined | ollama `nomic-embed-text` | 0.971 | 0.988 | 0.948 | 2.5 / 4.8 / 12.0 | 1.000 |

The model and dimension in the table are provider-reported: `capabilities()` equals
`EmbeddingResult.model`, and the dimension is read from the result.

Embedding latency for 46 pages in 3 batches of up to 16:

| Provider | Per batch | Total | Notes |
|---|---|---|---|
| mock | ≈ 8 ms | ≈ 24 ms | |
| nomic-embed-text | 1968 / 958 / 818 ms | 3.7 s | The first batch includes the model load. |

Observations:
- **Combined vs the best single channel, at recall@10.**
  - With mock embeddings, combined (0.996) is above taxonomy (0.988), which is the gated rule.
  - With nomic, combined ties taxonomy (0.988).
  - With nomic, combined **MRR (0.948) is below embedding-only MRR (0.968)**. The simple sum lets
    the coarse, tie-heavy taxonomy score override a better first hit from the embedding. Phase 3
    should tune channel weights on held-out labels, not assume equal weights.
- **nomic separates strong from weak far better than the mock** (embedding R@5 0.928 vs 0.788).
  Its cosine range is compressed, though: the mean is 0.85 for strong partners and 0.75 for
  unrelated ones. Calibrate `KEN_SIMILARITY_MIN_SCORE` (default 0.55) per model. At 0.55,
  98.9% of all nomic page pairs on this corpus clear the threshold (the minimum is 0.511).

## Regression floors (mock)

`test_retrieval_eval.py` sets each floor 0.05–0.10 below the measured mock value, rounded down to
0.05. They are a **regression gate, not a target**.

| Channel | R@5 | R@10 | MRR | Pairwise |
|---|---|---|---|---|
| taxonomy | 0.85 | 0.90 | 0.80 | 0.90 |
| embedding (mock) | 0.70 | 0.85 | 0.85 | 0.80 |
| combined | 0.85 | 0.90 | 0.85 | 0.90 |

The test also asserts two orderings:
- mean ranks run strong < weak < unrelated, and strong pairs score higher than unrelated ones on
  average, in every channel;
- combined recall@10 is at least the best single channel's.

Lowering a floor is a reviewed decision.

## Known spec conflict (recorded, not gated)

`test_market_rule_matches_adr_wording` checks ADR-0010 §3 literally: "`market_id` is the **most
specific** node whose score clears the confidence threshold". `analyze.py` picks the
**highest-scoring** confident node instead, preferring the deeper node only on a tie. The two rules
disagree on two pages, both strict `xfail`:

| Page | ADR wording gives | Implementation gives |
|---|---|---|
| `/industries/automotive` | `ev-charging` (8.0) | `automotive` (21.0) |
| `/industries/healthcare` | `diagnostics` (5.0) | `healthcare` (12.0) |

Verdict: the implementation's choice is editorially better (an industry hub is about the industry),
so the ADR wording needs a fix. The goldens deliberately do not assert `market_id` for hubs.

## Caveats

- **Synthetic corpus, fixture taxonomy.** The 65 pages and `taxonomy.v1` (`source: fixture`) were
  written together. The aliases appear in the text by construction, so absolute numbers
  overstate real-site quality. Rerun on Ken's real taxonomy and content before relying on any
  value (NEXT_STEPS).
- **The labels favour the taxonomy channel.** The `strong` rubric is phrased in terms of markets,
  geographies and declared relations, the same concepts the taxonomy channel scores. Treat the
  taxonomy numbers as a consistency check, and the embedding numbers as the informative ones.
- **Small sample.** With 42 queries, one query moves recall by about 0.02. Differences under
  roughly 0.03 are noise.
- **Exact cosine over 46 pages.** This measures quality, not ANN or pgvector performance.
- **The Ollama latency is from one laptop run.** It is indicative only.
