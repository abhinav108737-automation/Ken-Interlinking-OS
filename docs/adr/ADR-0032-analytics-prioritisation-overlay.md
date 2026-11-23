# ADR-0032 — GA4/GSC prioritisation without analytics approval

- **Status:** Proposed for offline/shadow comparison, 2026-11-22
- **Scope:** candidate retrieval and ordering; no deployment, approval or confidence change

## Context

The recommendation engine already imports page-level GA4 and Search Console metrics and exposes
an analytics retrieval channel, but recommendation policy v1 keeps that channel disabled. When
present, the signals were recorded and could block an unsupported analytics-only target, but they
could not change the order of otherwise eligible recommendations.

The owner requested a paragraph-level demonstration of how these signals change prioritisation.
First-party counts are still noisy and can be manipulated, so they must not create relevance,
bypass editorial evidence or act as an approval gate.

## Decision

1. Keep the eight-component relevance score unchanged. Hard geography, market, visibility,
   placement and evidence gates continue to run without analytics.
2. Require every candidate to pass the existing non-analytics `min_relevance` threshold before
   analytics can affect its position.
3. Add a versioned ordering overlay to ranking policy data. Proposed v2 uses:
   `0.70 × gsc_opportunity + 0.30 × ga4_engagement`, with a maximum ordering boost of `0.05`.
4. The overlay changes only the order in which already-eligible candidates consume selection
   caps. It does not change `ScoreBreakdown`, final plan score, confidence or any threshold.
5. Keep recommendation/ranking v1 as the configured baseline. The v2 files remain `proposed` and
   require the normal offline, shadow and human-promotion lifecycle before activation.
6. Preserve each raw signal, the combined analytics priority and the applied boost separately so
   a reviewer or evaluation can explain the movement.

## Consequences

- A close evidence-backed pair can move ahead of another when imported demand and engagement are
  stronger; a weak or ineligible pair cannot become eligible.
- Runs under v1 are byte-for-byte policy reproducible because v1 is not edited and its overlay
  defaults to disabled.
- Missing analytics produces a zero boost. Import delays therefore fall back to baseline order
  instead of failing the recommendation run.
- The overlay is deliberately small and proposed-only. The live comparison report must be judged
  for editorial quality before any promotion.

## Evidence

`docs/reports/ANALYTICS_RANKING_COMPARISON_2026-11-22.md` records the first live-data comparison.
For the Indonesia Car Rental source, the top two candidates reverse while their relevance and
final reviewer-facing scores remain unchanged.
