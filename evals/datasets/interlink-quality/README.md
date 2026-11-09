# interlink-quality — the owner's editorial rules, as 26 labelled cases

> **Status: declarative, and not yet wired.** The dataset is a file of labelled cases. There is
> no scorer and no runner entry: `ken_contracts.learning.EvalSuite` has no `interlink-quality`
> member, so `available_suites()` cannot see this directory and `make eval SUITES=all` ignores
> it. Every case validates against the frozen `EvalCase` in every other field, so adding that
> one enum member and a scorer is all that stands between this file and the gate.

## What it measures

Whether a proposed internal link would be *published*, under the four rules the project owner
stated on 2026-10-30 after reading the ten-plan interlinking baseline:

1. **Reader intent / geography.** Same country ALLOW · country↔containing region ALLOW ·
   any→global ALLOW · different countries in one region ALLOW only where the source text
   establishes the relationship (export, supply chain, named comparison) · different regions
   REFUSE.
2. **Cross-market is refused.** Only a same market, a taxonomy parent/child, or a declared
   supply-chain relation qualifies. Word overlap never does.
3. **The anchor is the target's market keyword** — not a bare noun — and names the target's
   geography when it differs from the source's.
4. **The anchor sits in running prose**, never inside a report title in a related-reports list.

Each case carries the source and target identity (real URL, real title, primary geography,
market), the proposed anchor with its verbatim surrounding sentence, the expected decision, and
for a refusal a reason code.

### Decision vocabulary

`allow`, or `refuse:<code>` where the code is one of:

| code | rule |
|---|---|
| `geography_unrelated_countries` | 1 — different countries, relationship not established in the source |
| `geography_different_region` | 1 — different regions |
| `cross_market` | 2 |
| `anchor_not_target_keyword` | 3 — bare noun, or not the target's market keyword |
| `anchor_missing_target_geography` | 3 — target geography differs and the anchor does not name it |
| `anchor_wrong_sense` | 3 — the phrase in this sentence does not denote the target's subject |
| `anchor_is_source_own_market` | 3 — the span is part of the source's own market name |
| `anchor_not_in_prose` | 4 |

Several rules usually refuse the same bad plan. `expected.decision` names the owner's primary
reason and `acceptable_alternatives` lists the other correct refusals, so a change that fixes
one filter cannot let a plan back in through another. `must_not` is zero-tolerance.

Each case declares a **`scope`** in `inputs.extra`, and asserts nothing outside it:
`pair_and_anchor`, `geography_gate`, `market_gate`, `geography_and_market_gate`, `anchor_only`.

## Where the cases came from

| Group | Cases | Source |
|---|---|---|
| Baseline plans the owner called **Poor** | 7 | `link_plans` in `ken_real500`, plan ids recorded per case |
| Baseline plans the owner called **Good** | 2 | same |
| The baseline plan the owner called **Acceptable** | 1 | same — a recorded conflict, non-scoring |
| The owner's four target-quality pairs, pair level | 2 (the other 2 appear in the rows below) | pages in `ken_full` |
| Anchor-availability findings for those four pairs | 4 | `page_versions.body_text` in `ken_full` |
| Geography-table boundary cases, one per row | 8 | real pages in `ken_full` / `ken_real500` |
| Rule-2 controls with geography held constant | 2 | real pages in `ken_full` |

Every URL, title, anchor span, block id, offset and context sentence is real and was read
read-only on 2026-10-30: the ten plans and their `preview` before/anchor/after windows from
`ken_real500`, page text from `page_versions.body_text` in both databases. Nothing was written.
No page text in this file was composed by the author: every `context`, `establishing_sentence`
and `best_available_context` value is quoted verbatim from the database. The `description` and
`rationale` fields are the author's prose, as the house format intends.

The owner's four target-quality pairs are split across two groups on purpose. Their *pairs* are
encoded ALLOW — `iq-pos-india-sun-protection-to-india-skincare-pair`,
`iq-pos-india-pickles-to-india-online-grocery-pair`, `iq-geo-same-country-allow` (Saudi freight →
Saudi contract logistics) and `iq-mkt-taxonomy-child-to-parent-allow` (global car fragrance →
global fragrance). Their *anchors* are a separate matter: checked against `ken_full`, three of
the four specified anchor phrases do not occur in the source's running prose at all, and the
fourth occurs only inside the source's own market name. Those four findings are the `iq-amb-*`
cases, and they are marked `needs_owner_ruling`.

## Cases that deliberately do not score

Six cases carry `inputs.extra.needs_owner_ruling: true`. They record a place where the rules as
written are ambiguous or collide, and they are not assertions that the recorded decision is
correct. `iq-amb-india-dry-fruits-to-africa-cashew` goes further and lists both outcomes as
passing, because the stated geography table refuses a plan the owner's verdict accepted.

Resolve them by amending the rules, then bump `fixture_version` on the cases that change.

## Honesty about what this is

**These labels are editorial judgement on a tiny sample, not a benchmark.** Specifically:

- 10 of the 26 cases come from one 10-plan baseline on 498 pages, chosen by the engine's own
  confidence ranking. The owner's verdicts on those ten (2 good, 1 acceptable, 7 poor) are the
  ground truth here, and they are one person's opinion recorded on one day.
- The remaining 16 were authored by the eval-learning-engineer agent from the owner's four
  target-quality pairs and from the geography table, over real pages. Their labels are the
  agent's reading of the owner's rules, not the owner's own verdicts.
- 26 cases cannot support a precision or recall estimate, a confidence calibration, or any
  claim about the 8,858-page corpus. A pass rate on this dataset means "the engine still agrees
  with the owner on these 26 situations", and nothing more.
- The region level used in `source.geo.region` and `target.geo.region` (for example "Middle
  East" for Saudi Arabia and Oman, "Asia Pacific" for India and Indonesia) is the **author's
  annotation**. The owner's rule 1 does not say at which level "the same region" is evaluated,
  and the answer changes which reason code applies to the Indonesia→India and Saudi→Oman cases
  (it does not change their outcome, which is REFUSE either way).
- `privacy.contains_production_data` is `true` on every case, with `approval_ref` naming the
  owner directive of 2026-10-30 that asked for real data. The content is public
  kenresearch.com report text; no personal data is included. This is the first dataset in
  `evals/datasets/` to carry that flag — every other suite is synthetic.

## Editing

Follow `evals/datasets/README.md`: edit the JSONL, run
`.venv\Scripts\python.exe evals/datasets/rehash.py --write`, and bump `fixture_version` plus the
manifest's `dataset_version` yourself when the *meaning* of a case changes. Never weaken an
expectation to make a case pass.
