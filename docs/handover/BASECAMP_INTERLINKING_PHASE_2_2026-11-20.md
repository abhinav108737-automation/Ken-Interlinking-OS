# Contextual Interlinking System — Phase 2

## What this is

An editorially controlled system that finds useful relationships between Ken Research reports,
drafts contextual internal links, scores their reader value, and publishes only human-approved
links through the CMS API. Every live change is version-locked, recorded, publicly verified and
reversible.

## Why it matters

Useful internal links help readers move from a broad market question to a more specific report.
They also give search and AI crawlers clear, descriptive paths between genuinely related pages.
The system is designed to improve information discovery and engagement without adding forced,
keyword-led or misleading links.

## How it works, in plain terms

1. Read the source and destination reports and identify the exact reader need.
2. Exclude Market Overview, Related Reports, headings, tables, citations and weak keyword matches.
3. Score the proposal across seven editorial KPIs and require source and destination evidence.
4. Show the exact before/after copy in the review dashboard for approval, revision or rejection.
5. Dry-run the approved change against the current CMS version.
6. Apply the link, refresh the public page, verify the rendered anchor and retain rollback details.
7. Record decisions and outcomes so future recommendations learn from editorial feedback.

## Current status

- The v0.2 reviewer and drafter use the contextual reader-value rules from the project knowledge base.
- The review dashboard shows seven KPI scores, evidence gates, exact copy and structured feedback.
- Seven new v0.2 recommendations were approved for controlled implementation. Four are now live
  and publicly verified; three canaries were rolled back because the selected CMS section was not
  rendered by the corresponding public page template.
- The CMS content-change API supports exact contextual replacements with version checks and rollback.
- Existing approved links and earlier live tests remain recorded in the project audit trail.

## What is deliberately not happening

- No link is published only because two reports share a keyword.
- Market Overview and Related Reports are excluded from contextual placements.
- A high editorial score cannot bypass missing evidence, a stale version or a technical gate.
- No model can publish without a recorded approval and controlled CMS workflow.
- A CMS success is not treated as complete until the link appears in public rendered HTML.

## Updated to-dos

- [x] Dry-run all seven approved v0.2 contextual links.
- [x] Publish and publicly verify four eligible links; roll back three non-rendering placements.
- [x] Record deployment IDs, final CMS versions and rollback handles in the audit record.
- [x] Add live deployment and rendered-verification status to each dashboard card.
- [ ] Select genuinely visible alternative sections for the three rolled-back links and return
  them to the dashboard for fresh approval.
- [ ] Run a post-publish audit for duplicate links, broken destinations and incorrect anchors.
- [ ] Capture baseline clicks and engagement for the seven source pages.
- [ ] Compare 14-day and 30-day link clicks, report-page engagement and assisted navigation.
- [ ] Build Phase 2 silo depth: category boundaries, commercial-priority routing and hub relationships.
- [ ] Add graph traversal for orphan pages, dead ends and weakly connected report clusters.
- [ ] Expand the evaluated corpus only after the seven-link canary produces clean quality signals.
