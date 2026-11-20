# v0.2 editorial feedback results — 2026-11-20

## Recorded outcome

- Three contextual recommendations were rejected: `contextual:1`, `contextual:2`, and
  `contextual:3`.
- The current set contains 33 proposals: 3 remain eligible for fresh editorial review and 30 are
  excluded by the revised rules.
- The remaining review candidates are `contextual:8`, `contextual:12`, and `contextual:13`.
- No production content was changed or rolled back during this review.

## Learned rules

1. Market Overview must discuss only the report's stated market. It cannot be edited or used to
   introduce an interlink to another market.
2. Related Reports cannot host inline interlinks or another regional-sibling module because the
   reports are already presented there.
3. Topical relationship alone is insufficient. The surrounding sentence must clearly explain the
   additional information the reader receives by clicking.
4. Anchor text must clearly predict the destination market. A vague or partial capability phrase
   is not sufficient.
5. The copy must remain useful and natural when the hyperlink is removed. Text added mainly to
   manufacture a link is pushy and must be rejected.
6. Bot crawlability must come from coherent, descriptive connections; it is not a reason to add
   more links.

## Implementation result

- Market Overview and Related Reports are deterministic placement exclusions.
- Partial destination-market anchors are disabled in the recommendation pipeline.
- The regional-sibling module builder is disabled and will clear older derived module rows on its
  next run.
- Excluded proposals remain available in the dashboard as learning evidence but cannot be approved
  or queued.
- The dashboard defaults to eligible proposals only.

## Live exceptions requiring a separate decision

- `contextual:1`, deployment `18`: live, rejected, and now outside policy.
- `contextual:7`, deployment `19`: live and now outside policy; editorial decision still pending.

Neither deployment was automatically rolled back. Production rollback remains an explicit,
auditable action.

## Verification

- Dashboard API: 33 proposals, 3 eligible, 30 excluded, 3 saved rejection decisions.
- Approval of a policy-excluded proposal is refused by the API.
- JavaScript and Node server syntax checks passed.
- Workbook formula/error scan found no errors.
- Python tests were not executed because the local Python installation referenced by `.venv` is
  unavailable. This remains a verification requirement before commit or deployment.

## Governing references

- `knowledge/FULL_CONTEXTUAL_INTERLINKING.md`
- `docs/adr/ADR-0031-editorial-section-and-engagement-gates.md`
- `knowledge/contextual-interlinking-decisions.jsonl`

