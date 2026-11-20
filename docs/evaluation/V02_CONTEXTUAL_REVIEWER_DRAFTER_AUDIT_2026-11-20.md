# v0.2 contextual reviewer and drafter audit — 2026-11-20

## Outcome

The current CMS copy was audited read-only after applying the approved contextual-linking rules.
Of 33 original proposals, 30 remain policy-excluded. The three eligible proposals pass the stricter
review. All three are already live and verified, so the safe draft is to retain the existing link
and make no further wording change.

| ID | Source → destination | Placement and anchor | Result | Why it passes |
|---|---|---|---|---|
| contextual:8 | Procurement Spend Control → Document Management Systems | Growth Drivers; `document management` | Retain | A visible list item connects supplier registration/renewal workflows to recurring document-management needs; the destination discusses recurring document-control services. |
| contextual:12 | Retail E-Commerce Analytics → Retail Inventory Forecasting | Segmentation explanatory prose; `Demand Forecasting and Inventory Analytics` | Retain | The paragraph explains replenishment, availability, working-capital and profit decisions; the destination explains forecast-to-replenishment and inventory availability. |
| contextual:13 | Retail Supply Chain AI → Retail Inventory Forecasting | Segmentation explanatory prose; `Demand Forecasting Platforms` | Retain | The paragraph explains promotions, store variability, purchasing commitments and replenishment; the destination covers demand forecasting across retail channels and inventory allocation. |

## Reviewer changes

- Requires both reader questions: why the page is related and what extra information it provides.
- Rejects pushy, manufactured, vague, unsupported or broad-keyword-only connections.
- Pins Market Overview, Related Reports, tables, headings, citations, methodology, legal copy and
  boilerplate FAQs as unacceptable placements.
- Requires a sentence that stays useful when the link is removed.
- Includes the source block kind in the critic evidence envelope.

## Drafter guardrails

- Uses current source and destination evidence and records the source field version.
- Accepts only visible paragraphs or complete explanatory list items.
- Prefers no copy change when the existing context is sufficient.
- Refuses to invent a fact, number, entity, geography or market claim.
- May preview one publicly visible destination fact when it directly answers the source reader's
  next question; it must record the exact destination evidence and avoid unsupported causality.
- Produces a shadow record only; it does not validate, apply or publish through the CMS.

Machine-readable and reviewer-friendly outputs:

- `outputs/contextual-v02-shadow/v02_contextual_audit_results.json`
- `outputs/contextual-v02-shadow/v02_contextual_audit_results.md`

No CMS write was performed during this audit.
