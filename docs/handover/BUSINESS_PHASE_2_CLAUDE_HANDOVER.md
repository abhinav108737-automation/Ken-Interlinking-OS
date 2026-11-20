# Claude handover — Business Phase 2 priority interlinking

- **Date:** 2026-11-19
- **Source of product intent:** `knowledge/BUSINESS_PHASE_2_PRIORITY_INTERLINKING.md`
- **Scope:** local planning and implementation in this repository
- **Not authorised:** production CMS writes, live deployment, policy promotion, remote push, paid
  APIs, or changes to the approved permission model

## Handover instruction

Read the mandatory repository documents in `START_HERE.md`, then read:

1. `knowledge/README.md`
2. `knowledge/BUSINESS_PHASE_2_PRIORITY_INTERLINKING.md`
3. `NEXT_STEPS.md`, especially items 29, 31, 32, and 34
4. the relevant ADRs and implementation files referenced by those items

Treat the knowledge-base document as approved business and editorial intent. Do not treat it as
permission to bypass frozen contracts, ADR governance, safety gates, or production permissions.

## First task

Perform a repository-backed gap analysis before editing implementation code:

- map every required capability to what already exists;
- identify reuse, extensions, genuine gaps, and conflicts;
- distinguish configuration/policy changes from schema, service, UI, evaluation, and analytics work;
- identify decisions that need an ADR or owner input;
- propose the smallest safe Business Phase 2A vertical slice;
- specify tests, evaluation fixtures, migrations, and measurable acceptance criteria.

Write the result to `docs/handover/BUSINESS_PHASE_2_GAP_ANALYSIS.md` and update `NEXT_STEPS.md` with
the agreed sequence. Do not create a second recommendation engine.

## Implementation direction after the gap analysis

Unless the gap analysis uncovers a governing conflict, start with Business Phase 2A:

1. governed priority-page designations that cannot bypass existing gates;
2. explicit, versioned page-family routing for the business journey;
3. topic-hub membership and explainability;
4. evaluation fixtures for priority, hub, cross-product, geography, stale, and protected cases;
5. shadow-mode output only.

Work one vertical slice at a time and follow the repository's phase discipline. Stop for any ASK or
BLOCK action. Do not touch production.

## Important terminology

"Business Phase 2" is the new engagement expansion described in the knowledge base. It is not the
repository's historical software Phase 2 and must not overwrite `PHASE_2_REPORT.md`.
