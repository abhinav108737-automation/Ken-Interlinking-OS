# v0.2 contextual interlinking shadow implementation

Date: 2026-11-19

## What is implemented

- The complete current v0.2 inventory of 506 reports is captured in one review workbook.
- Fifteen manually reviewed Kuwait Cloud-Based contextual links are structured as source, destination, exact placement, proposed anchor/copy, reader value, and deployment state.
- Five already-live links are separated from ten pending contextual edits.
- Nine exact market-core regional groups are retained as relationship evidence, producing 18
  directional sibling pairs. They are excluded from recommendation until one side contains an
  allowed, genuinely informative inline placement.
- Every pending decision has an `Approve`, `Revise`, or `Reject` field so editorial feedback can be recorded as training evidence.

## Guardrails applied

- A contextual sentence may be added only when the surrounding paragraph already discusses the destination capability or a directly dependent workflow.
- The added sentence must remain useful even if its link is removed.
- The anchor describes what the reader will find; it is not a forced exact-match keyword.
- Market Overview and Related Reports are hard exclusions.
- A regional sibling becomes an inline link only when allowed explanatory prose actually names or
  compares that destination geography and explains what the reader gains.
- No new live content was changed in this shadow implementation.

## Review and release sequence

1. Review the 10 pending contextual edits and 18 regional directions.
2. Record `Approve`, `Revise`, or `Reject` plus notes in the workbook.
3. Dry-run approved changes through the content-change API.
4. Release a maximum five-page canary.
5. Verify public rendering, record deployment and rollback IDs, then expand in batches.

## Artifacts

- Review workbook: `outputs/contextual-v02-shadow/v02_contextual_interlinking_shadow_review.xlsx`
- Repeatable builder: `run/contextual-v02-shadow/build_shadow_workbook.mjs`
- Visual and structural QA: `run/contextual-v02-shadow/previews/`

