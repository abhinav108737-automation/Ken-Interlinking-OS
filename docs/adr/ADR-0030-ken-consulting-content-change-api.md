# ADR-0030 — Ken Consulting content changes for contextual links

- **Status:** Accepted and observed, 2026-11-19.
- **Decision owner:** the project owner, who confirmed that Snehal granted content-edit API
  permission and instructed that changes and optimisation lessons be recorded in the project.
- **Extends:** ADR-0028 at the Ken Consulting adapter boundary.
- **Does not implement:** autonomous copy editing or the full ADR-0023 plan kind.

## Context

Deployment 20 inserted `document management` into a CMS description that the public Growth Drivers
component did not display. The CMS and serialized page data contained the link, but readers could
not see it. Fixing the placement required a small content edit in a visible bullet.

Snehal supplied the production API contract after the route permission was enabled. Treat that
handover as the contract reference, not as authority to bypass project controls.

## Observed contract

| Operation | Endpoint | Required body |
|---|---|---|
| Validate replace/insert | `POST /internal/pages/{slug}:{field}/content-changes/validate/` | `expected_version`, `change` |
| Apply replace/insert | `POST /internal/pages/{slug}:{field}/content-changes/` | `operation_id`, `expected_version`, `change` |
| Roll back | `POST /internal/deployments/{deployment_id}/rollback/` | none |

`replace` uses `find` and `new_text`. `insert_after` uses `after_text` and `sentence`. New content
may contain plain text and `<a href="…">` markup. The apply endpoint is idempotent by
`operation_id`, version-locked, and returns a normal deployment ID.

## Decision

1. Every content edit is dry-run first.
2. A contextual copy-and-link repair should be one atomic content deployment when the API can carry
   the anchor in `new_text`.
3. Public verification must inspect a rendered anchor, not only raw CMS content or serialized data.
4. Until ADR-0023 is implemented, content edits remain explicit, one-page operator actions; the
   recommendation engine does not gain autonomous copy-writing authority from this ADR.
5. Scripts and audit records must store exact before/after wording and every deployment/rollback ID.

## Live evidence

- Deployment 20: original hidden link; rolled back.
- Deployment 23: compensating restoration after an HTTP 422 contract mismatch; rolled back.
- Deployment 24: validated atomic visible copy-and-link repair; applied and publicly verified.
- Final public result: exactly one rendered `document management` anchor to the Kuwait Cloud-Based
  Document Management Systems report.
