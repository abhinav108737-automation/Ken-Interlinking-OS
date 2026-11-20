# v0.2 new eligible contextual drafts — 2026-11-20

## Audit result

- Restored Python 3.12.10 at the user-local path required by the project virtual environment.
- Scanned all 506 v0.2 reports using current CMS content.
- Found 208 literal same-geography evidence matches and four exact-market regional mentions.
- Applied the contextual reader test, destination-evidence check and hard placement exclusions.
- Retained seven new drafts: five existing-text links and two small regional anchor edits.
- Performed no CMS validation, apply or publication.

| ID | Source → destination | Placement | Anchor | Draft type | Reason |
|---|---|---|---|---|---|
| v02-new:1 | Kuwait Electrosurgical Devices → Kuwait Electrosurgical Generators | Segmentation prose | `Electrosurgical Generators` | Existing text | The source explains generator procurement, console standardization and installed-base value. |
| v02-new:2 | Mexico Cold Chain and Food Storage → Mexico Pharmaceutical Logistics | Growth Drivers | `Pharmaceutical logistics` | Existing text | The source explains validated handling across several temperature environments. |
| v02-new:3 | Kuwait Germanium → Kuwait Fiber Optics | Segmentation prose | `Fiber Optics` | Existing text | The source connects fiber expansion and cable qualification to germanium demand. |
| v02-new:4 | Kuwait Female Infertility Diagnosis → Kuwait Genetic Testing | Growth Drivers | `genetic testing` | Existing text | The source explains specialist referrals without every clinic operating a genetic laboratory. |
| v02-new:5 | Kuwait Funeral Homes → Kuwait Funeral and Repatriation Services | Segmentation prose | `Repatriation Services` | Existing text | The source identifies repatriation as a separate case-coordination capability. |
| v02-new:6 | Latin America Cargo Security → Mexico Cargo Security | Regional Analysis | `Mexico Cargo Security Market` | Small edit | The source directly compares Mexico on market scale and cross-border freight. |
| v02-new:7 | Latin America Warehousing → Mexico Warehousing | Regional Analysis | `Mexico Warehousing Market` | Small edit | The source directly compares Mexico's ranking, growth and cross-border inventory. |

## Editorial scorer reference

| ID | Score | Reference outcome | Confidence |
|---|---:|---|---:|
| v02-new:1 | 96/100 | Approve | 95% |
| v02-new:2 | 97/100 | Approve | 95% |
| v02-new:3 | 97/100 | Approve | 94% |
| v02-new:4 | 94/100 | Approve | 88% |
| v02-new:5 | 99/100 | Approve | 97% |
| v02-new:6 | 98/100 | Approve | 96% |
| v02-new:7 | 97/100 | Approve | 94% |

These are independent editorial reference scores across reader need, source evidence, destination
fit, anchor expectation, context and placement, added information value, and quality/risk. They
are not Google scores or AI-citation predictions. The rendered crawlability gate was pending at
scoring time; the controlled publish results below provide the final gate outcome.

## Controlled publish outcome — 2026-11-20

| ID | Outcome | Deployment IDs | Public result |
|---|---|---|---|
| v02-new:1 | Rolled back | 26 | Approved segmentation placement was not rendered publicly. |
| v02-new:2 | Rolled back | 27 | Approved Growth Drivers placement was not rendered publicly. |
| v02-new:3 | Live | 28 | CMS and public anchor verified. |
| v02-new:4 | Rolled back | 29 | Approved Growth Drivers placement was not rendered publicly. |
| v02-new:5 | Live | 30 | CMS and public anchor verified. |
| v02-new:6 | Live | 31, 32 | Regional copy expansion and public anchor verified. |
| v02-new:7 | Live | 33, 34 | Regional copy expansion and public anchor verified. |

The three failed canaries were automatically reversed. They need a newly evidenced, visibly
rendered placement and fresh approval; a successful CMS write alone is not sufficient.

## Verification

- Reviewer prompt and critic tests: 71 passed.
- Complete agent unit-test folder: 243 passed.
- Ruff passed for the reviewer and both new audit scripts.
- Every draft was re-read from the current CMS, checked for an existing inline link, and matched
  to explanatory destination evidence in an approved field.

The machine-readable draft record is
`outputs/contextual-v02-shadow/new_eligible_interlinking_drafts.json`; the reviewer-readable copy
is `outputs/contextual-v02-shadow/new_eligible_interlinking_drafts.md`.
