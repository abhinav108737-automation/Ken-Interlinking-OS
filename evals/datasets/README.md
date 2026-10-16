# Eval datasets — Phase 8

> **Status: implemented.** Ten suites, versioned JSONL, hashed manifests, a runner and a gate.
> `editorial-agreement` is deliberately absent — see below.

Layout, frozen by [ADR-0016](../../docs/adr/ADR-0016-learning.md) §7 and
[`evals/README.md`](../README.md) §4:

```
evals/datasets/<suite>/<dataset>.v<N>.jsonl   ← one EvalCase per line
evals/datasets/<suite>/MANIFEST.yaml          ← DatasetManifest: version, per-file sha256,
                                                 case count, provenance, privacy
evals/datasets/rehash.py                      ← re-hash a manifest after editing a case
```

**Datasets are files; runs are rows.** A fixture belongs in a reviewable commit and is
reproducible from a checkout; PostgreSQL is the system of record for what a *run* measured
(`policy_evaluations`). Every case validates against the frozen
`ken_contracts.learning.EvalCase`, and the runner refuses a dataset whose bytes no longer match
its manifest — so a recorded `evaluation_ref` always names a dataset whose contents are known.

## Running

```bash
make eval SUITES=all                                  # the full gate
make eval SUITES=protected-content,proposal-safety    # one or more suites
.venv/bin/python evals/runner.py --suites all --no-db # no job run, just the report
```

Default provider is `mock`: recorded responses are replayed by role and embeddings are computed
by deterministic feature hashing, so the same datasets and seed produce a **byte-identical**
report and CI can gate on it. `--provider ollama` needs `KEN_RUN_LIVE_OLLAMA=1`, talks only to
the local daemon, and its gate result is forced to `inconclusive` — a live model may inform a
decision, never gate one.

## What the cases are made of

Every case is **synthetic**: invented Ken-Research-*shaped* market pages over the committed
`policies/taxonomy/taxonomy.v1.yaml`. `privacy.contains_production_data` is `false` on every
case in every suite, and every manifest declares the same. Using real production data requires
a recorded `approval_ref` (`evals/README.md` §4), which this phase does not have.

Identifiers are derived, never invented: a case names a page by a readable key and the page id
is `uuid5(NAMESPACE_URL, "ken-eval:page:<key>")`, which is why the report is reproducible across
machines. Content blocks are parsed from the fixture's HTML by the production normaliser, so a
case cannot declare block text its markup does not produce — and a fabricated anchor is
structurally impossible.

## Case counts and what each suite genuinely covers

| Suite | Cases | Gate | Drives |
|---|---|---|---|
| [destination-selection](../suites/destination-selection/README.md) | 17 | ≥ 0.80 | `pair_rejection` → `score_pair` under the committed policies |
| [anchor-selection](../suites/anchor-selection/README.md) | 14 | ≥ 0.80 | `anchor_phrases` + `find_placement` over parsed HTML |
| [relationship-detection](../suites/relationship-detection/README.md) | 13 | ≥ 0.75 | `taxonomy_relationships` |
| [critic](../suites/critic/README.md) | 12 | ≥ 0.75 | the real `ken_agents.nodes.critique` node |
| [no-action](../suites/no-action/README.md) | 11 | ≥ 0.90 | the real `triage` node, plus the selection floors |
| [protected-content](../suites/protected-content/README.md) | 14 | **hard fail** | the pair chain, `plan_invalidity`, the CMS adapter |
| [stale-page-version](../suites/stale-page-version/README.md) | 16 | **hard fail** | `check_plan_freshness`, `HtmlMutator`, the optimistic lock |
| [policy-regression](../suites/policy-regression/README.md) | 12 | ≥ 0.90 | candidate ranking policies vs recorded preferences |
| [reflection-grounding](../suites/reflection-grounding/README.md) | 13 | **hard fail** | re-derivation of claimed evidence from a data snapshot |
| [proposal-safety](../suites/proposal-safety/README.md) | 31 | **hard fail** | `PolicyChange` / `PolicyProposalBody`, `code_only_violations` |

A hard-fail suite cannot be waived by any policy value, and a `must_not` hit anywhere fails the
gate at zero tolerance.

**Every suite contains at least one control case** — a pair that *is* allowed, a plan that *is*
fresh, a proposal that *is* accepted, a triage that *does* return OPTIMIZE. Without them a
filter that refused everything would score 100% on a zero-tolerance suite, which is the only
way a gate like this can be gamed.

### What these suites do NOT cover

Stated plainly, because an eval suite's honest limits matter more than its pass rate:

- **No real editorial data.** Agreement with *recorded human decisions* is approximated by
  hand-authored `preferred` orders, not by held-out `review_actions`. That is why
  `editorial-agreement` is deferred (ADR-0016 §15.3) rather than stubbed.
- **No end-to-end pipeline run.** The suites drive the decision functions, not `make recommend`
  over a database. Integration coverage lives in `tests/integration/`.
- **No retrieval coverage.** Candidate *sets* are given per case; nothing here measures
  `semantic_k`/`lexical_k` recall. `evals/recommendations/benchmark.py` covers that.
- **Calibration is a mechanism demonstration.** Reliability bins and ECE are computed per
  decision class from the raw confidence a case produced, and **reported, never gating**. On
  ~65 synthetic pages they calibrate nothing.
- **`by_decision_class` carries no editorial rates.** `approval_rate`, `edit_rate`,
  `reject_rate`, `rollback_rate` and `positive_outcome_rate` stay `null` in an eval run: those
  come from the editorial and behavioural streams via `aggregate.py`. Filling them with eval
  pass rates would be the exact category error this phase exists to prevent.
- **Two `policy-regression` cases PIN a known regression** on purpose (a geography-blind and a
  commercial-only candidate). They assert the regression still happens, so it cannot quietly
  disappear; they are not assertions that the candidates are good.

## Editing a dataset

1. Edit the `.jsonl` (one JSON object per line, `case_id` stable and never reused).
2. Run `.venv/bin/python evals/datasets/rehash.py --write`.
3. If the *meaning* changed rather than a typo, bump the case's `fixture_version` and the
   manifest's `dataset_version` yourself — a past evaluation row points at that version.
4. Re-run `make eval SUITES=<suite>`.

Never weaken an expectation to make a case pass. A failing case is either a real regression or
a mis-authored fixture, and those are different conversations.
