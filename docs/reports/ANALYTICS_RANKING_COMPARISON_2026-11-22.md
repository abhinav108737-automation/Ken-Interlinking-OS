# GA4/GSC recommendation-ranking comparison — 2026-11-22

## Result

Proposed policy v2 changes the review order for a real paragraph-level recommendation set while
leaving every hard gate, relevance threshold, plan score and confidence unchanged. Baseline v1
remains active and reproducible; v2 is `proposed` and has not been promoted.

## Method

- Database: `ken_full`.
- Analytics window: latest 28 imported days through 2026-11-19.
- GSC opportunity: normalised impressions multiplied by one minus click-through rate.
- GA4 engagement: normalised pageviews.
- Analytics priority: `0.70 × GSC opportunity + 0.30 × GA4 engagement`.
- Bounded ordering score: `relevance + 0.05 × analytics priority`, capped at 1.
- Eligibility and final plan score continue to use the original non-analytics relevance and
  placement quality. Analytics cannot make a candidate pass.

## Sample: Indonesia Car Rental Market

Source: `https://www.kenresearch.com/industry-reports/indonesia-car-rental-market`

| Candidate and source placement | Baseline rank | v2 rank | Relevance | GSC | GA4 | Analytics priority | Ordering score |
|---|---:|---:|---:|---:|---:|---:|---:|
| Global Car Rental Market — list item: “Indonesia **Car Rental Market** Size, Share & Forecast…” | 2 | 1 | 0.807819 | 0.033632 | 0.004673 | 0.024944 | 0.809066 |
| Malaysia Car Rental Market — list item: “**Malaysia Car Rental Market** Size, Share & Forecast…” | 1 | 2 | 0.808032 | 0.000000 | 0.002974 | 0.000892 | 0.808076 |
| Indonesia Car Rental & Leasing article — paragraph: “The **Indonesia Car Rental Market** serves leisure travelers, corporate fleets, government users…” | 3 | 3 | 0.748606 | 0.001427 | 0.004036 | 0.002210 | 0.748717 |

The first two candidates reverse order because their evidence-backed relevance is nearly equal,
but the Global destination has materially stronger GSC opportunity. The third remains third.

## Interpretation and guardrail

This demonstrates prioritisation, not automatic approval. The sample also shows why the overlay
is deliberately small and proposed-only: traffic demand can move a close decision, but it cannot
repair weak context, hidden placement, an inaccurate anchor or missing destination evidence.
Those remain independent blockers, and the reviewer still sees the unmodified final plan score.
