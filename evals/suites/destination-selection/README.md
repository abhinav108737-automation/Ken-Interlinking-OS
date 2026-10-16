# Suite: destination-selection

> Phase 0: structure only, with no cases. First populated in **Phase 3**
> (deterministic ranking baseline) and extended in **Phase 4** (Discovery agent
> fixtures). It becomes a formal dataset in Phase 8. TRD §19.

**Exercises:** `RETRIEVE` → `FILTER` → `RANK` (Discovery agent plus the
ranking policy, TRD §10–§12).

**A passing case:** the expected destination is in the top-k ranked
candidates, no `must_not` destination is selected, and every selected
destination passed deterministic eligibility (PRD §7.5).

**Example cases (prose):**
- A country-level market report should rank its regional parent-market report
  above a semantically similar page from an unrelated industry.
- A candidate the source already links to must not be selected again.
- A strong semantic match that is unpublished or has a non-canonical URL must
  be filtered before ranking.

**Failure modes guarded:** topical drift, i.e. ranking on embeddings alone
while ignoring taxonomy and geography; duplicate links; self-links; ineligible
destinations that leak past `FILTER`; ranking weights hardcoded instead of
read from versioned policy data (TRD §12).

## Phase 8 status: populated (17 cases)

Dataset: [`evals/datasets/destination-selection/core.v1.jsonl`](../../datasets/destination-selection/core.v1.jsonl).
Gate: `min_pass_rate` 0.80, `max_regression_vs_baseline` 0.05.

Drives the production path for real: `filters.pair_rejection` first (so protection and
eligibility are decided before any score exists), then `scoring.score_pair` under the committed
`recommendation.v1.yaml` and `ranking_weights.v1.yaml`, then the `min_relevance` floor, then
relevance descending with the target key as a stable tiebreak. The decision is the top-ranked
destination (`select:<key>`) or `no_eligible_candidate`.

**`must_not` means "never among the selected destinations"** — not "never ranked first". Every
filter case uses it: self-link, already-linked, open-plan duplicate, cooldown, draft, noindex,
non-canonical, thin, gone, the home page and a transactional page. Four preference cases carry
a recorded `preferred` order and are the only cases that feed precision@1, recall@3 and
Kendall tau; a ranking metric averaged over cases with no recorded preference would be a number
about nothing.

Covered: all ten pair-filter branches, source-side ineligibility (a hub may not carry links),
and the no-candidate outcome. **Not covered:** retrieval recall (candidate sets are given per
case) and anything requiring real approved-destination history.
