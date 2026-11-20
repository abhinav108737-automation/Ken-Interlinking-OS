# Full contextual interlinking pilot

- **Pilot corpus:** the 28 Kuwait Cloud-Based v0.2 reports already reviewed.
- **Authority:** owner direction on 2026-11-19 and ADR-0023.
- **CMS capability:** ADR-0030 content-change validate/apply/rollback endpoints.
- **Mode:** shadow proposals first; operator-approved canary writes only.

## Outcome

Produce useful inline links that preserve the source report's meaning and give the reader a clear
reason to continue to the destination report.

## Delivery sequence

1. **Rendered-section map** — identify which parts of every CMS field actually appear publicly.
2. **Candidate routes** — exact regional siblings, parent/submarket and direct workflow neighbours.
3. **Evidence bundle** — source paragraph, target evidence and intended reader benefit.
4. **Choose the least invasive mode** — existing text, micro-edit, bridge sentence, module or no
   link.
5. **Generate shadow previews** — exact before/after, anchor, destination and reason; no writes.
6. **Human review** — approve wording and destination, reject weak or unnecessary links.
7. **Five-page canary** — dry-run, apply, cache revalidation and rendered-DOM verification.
8. **Review outcomes** — accuracy, readability, visibility, rollbacks and early click data.
9. **Expand in batches** only if the canary has no factual or placement failures.

## Pilot limits

- Maximum three new contextual inline links per report.
- Maximum one new link per paragraph or explanatory list item.
- No reciprocal inline links merely for symmetry.
- Exact same-market regional reports receive a module by default; inline links require a paragraph
  that names or genuinely compares the other geography.
- No live write without a successful content/link validation preview.

## Proposal schema

| Field | Meaning |
|---|---|
| source / destination | Exact report URLs |
| relationship | Same market, parent/submarket, workflow dependency or answered question |
| field / placement | CMS field plus visible paragraph/list item |
| mode | Existing text, micro-edit, bridge sentence or module |
| find | Exact current source text used by the version-locked edit |
| before / after | Complete reviewable local copy |
| anchor | Words that become the link |
| evidence | Source and target spans supporting the wording |
| reader value | What useful information the click provides |
| risk | Geography, claim, tone and visibility checks |

## Acceptance gates

A proposal passes only when:

- destination relevance is explicit and evidence-backed;
- the source market's meaning is unchanged;
- every new fact is traceable;
- the anchor predicts the destination accurately;
- the placement is publicly rendered;
- the copy reads naturally without the link styling;
- rollback can restore the exact original text.

## Measurement

Track rendered-link success, review approval/rejection reasons, rollback rate, internal-link clicks
and destination discovery. Compare contextual copy with plain links and holdouts where possible.
Report associations only; do not claim that a link caused ranking or traffic changes.

## Implementation boundary

The existing deterministic context-anchor work in ADR-0029 remains unchanged. This pilot adds the
separate ADR-0023 copy-proposal path. Do not weaken market or geography gates and do not merge model
wording directly into production without validation and operator approval.
