# v0.2 interlinking review dashboard

Internal, local-only editorial dashboard for the current v0.2 contextual and regional-sibling proposals.

It also reads `outputs/contextual-v02-shadow/new_eligible_interlinking_drafts.json` and places
every `draft_ready` item at the top of the pending approval queue. New draft cards show current
copy, proposed linked copy, destination evidence, expected source version and validation status.

Each scored draft also shows the contextual editorial reference: a 100-point total, all seven KPI
scores, reviewer confidence and five independent evidence/technical gates. This reference is not
a Google ranking score or an AI-citation prediction. A score never overrides a failed gate, and
rendered crawlability remains pending until the approved link is applied and verified.

Kuwait topic-cluster cards also show a separate authority and indexability panel. It checks source
followability, destination HTTP status and `noindex`, canonical href alignment, and public content
presentation. These are hard gates, not bonus points. Google index and ranking status remain
unknown unless Search Console data is connected.

The dashboard also reads `data/v02-contextual-review/analytics-ranking-comparison.json` and shows
the proposed GA4/GSC v2 review order beside the relevance-only baseline, including the exact
paragraph or list-item placement used for each candidate. This comparison changes review order
only; it does not bypass any editorial or technical gate. Override the file with
`KEN_REVIEW_ANALYTICS_COMPARISON_PATH` when validating a different sample.

The same panel describes the proposed v3 directional authority-transfer signal. It combines a
source page's observed GSC visibility and GA4 attention with a target page's observed search
weakness. Missing GSC data remains unknown; it is not presented as proof of non-indexation.

## Start

1. Refresh the proposal dataset and workbook:

   `node run/contextual-v02-shadow/build_shadow_workbook.mjs`

2. Start the dashboard:

   `npm run dashboard:v02`

3. Open `http://127.0.0.1:4177`.

## Persistence

- Current decision state: `data/v02-contextual-review/decision-state.json`
- Append-only learning log: `knowledge/contextual-interlinking-decisions.jsonl`

Reject requires both a structured reason and an explanation. Revise requires revision instructions. Approval does not change the live site; a separate queue action marks an approved proposal for its controlled next step.
For a new shadow draft, the next step is dry-run CMS validation, not publication.

