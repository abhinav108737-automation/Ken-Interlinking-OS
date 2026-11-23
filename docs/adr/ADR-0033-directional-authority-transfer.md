# ADR-0033 — Directional authority transfer from GA4/GSC

**Status:** Proposed
**Date:** 2026-11-22

## Context

The recommendation engine already imports page-level GA4 and Search Console metrics and uses a
bounded analytics overlay to order relevant, eligible candidates. A second use is directional:
a page with proven organic visibility and real user attention can be a valuable source of a
contextual link to a relevant page with observed ranking weakness.

Traffic or ranking strength is not transferable as a guaranteed quantity. Analytics also cannot
prove that a page is indexed when Search Console has no row for it. Missing data may mean no
demand, an unresolved URL, incomplete coverage, or an unindexed page.

## Decision

Add three separate, explainable signals to the analytics candidate channel:

- `source_authority`: 80% GSC visibility/click/position strength and 20% GA4 pageview share;
- `target_search_need`: 60% observed average-position weakness and 40% no-click rate;
- `authority_transfer`: `source_authority × target_search_need`.

`ranking_weights.v3` may use `authority_transfer` as 35% of its bounded analytics overlay. The
overlay remains capped at 0.05 and executes only after indexability, relevance, evidence,
placement, duplication, and editorial gates. It changes review order only; it does not change
the relevance score, final score, confidence, eligibility, or approval state.

No GSC impressions means `target_search_need = 0` (unknown), not maximum need. Claims about
index status require separate URL Inspection evidence. Orphan and underlinked recovery remain
independent graph signals and can surface such pages for review without pretending analytics
proved their index state.

## Consequences

- Strong sources preferentially surface relevant weak targets instead of indiscriminately
  linking to low-performing pages.
- Every recommendation remains auditable through the source strength, target need, and product
  signal rather than a single opaque authority score.
- GA4/GSC data can prioritize work but cannot justify a link on its own or guarantee indexing or
  ranking improvement.
- Version 1 remains active; versions 2 and 3 remain proposed comparison policies.
