# ADR-0028 — Speaking the Ken Consulting CMS dialect at the adapter boundary

- **Status:** Accepted for implementation, 2026-11-12. Writes remain disabled.
- **Decision owner:** the project owner, who instructed on 2026-11-12: *"let's comply with what
  they have sent … keeping our functionality same, our logic same, and doing the implementation"*,
  and asked for blockers to be raised rather than worked around silently.
- **Amends:** [ADR-0014](ADR-0014-controlled-deployment.md) §4 and §8 — the production CMS is no
  longer assumed to implement the contract in `docs/handover/CMS_INTEGRATION_REQUIREMENTS.md`
  verbatim. It implements a near neighbour, and the difference is absorbed by a second adapter.
- **Does NOT amend:** `contracts/python/ken_contracts/cms.py`. The frozen contract stands
  unchanged, and so do the deployment service, the plan schema and the review flow.
- **Authority:** CLAUDE.md — a real CMS integration and a new provider are architecture changes
  and need a record.

## Context

The technology team delivered a working internal API on 2026-11-12 at
`https://www.thekenconsulting.com/app/product/internal`, with a read key and a write key. It
provides everything the October handover asked for — read, validate, apply, rollback, a
`protected` flag, an opaque `version`, and a 409 on version mismatch — plus a page-list endpoint
and a `related` block we had not asked for.

It is not, however, the contract we wrote `HttpCMSProvider` against. Seven differences matter:

| # | Their API | Our contract |
|---|---|---|
| 1 | Every path ends in `/` | No trailing slash, and the client never follows redirects |
| 2 | The unit of edit is `<slug>:<field>`, one of a report's 12 fields | One page, one body |
| 3 | Locator is `anchor_text` + 1-based `occurrence` | `block_selector` + 0-based `occurrence_index` + `context_hash` |
| 4 | `validate` is a collection endpoint taking `changes[]` | Per-page, same body as apply |
| 5 | `apply` takes a single `link` object | `changes[]`, one or more, applied atomically |
| 6 | No `actor`, no `dry_run`, no `change_id` | All three required |
| 7 | Two keys, read and write | One token |

## Decision

**1. A second provider, not a changed contract.** `KenConsultingCMSProvider` implements
`CMSProvider` and translates in both directions. Everything above the adapter — plans, locators,
the deployment service, the reviewer flow, rollback semantics — is untouched. This is the whole
point of having had a provider protocol: the dialect stops at the boundary.

`HttpCMSProvider` stays as it is. It is the adapter for the contract as published, it is
contract-tested, and it remains the reference for anyone else implementing the handover document.

**2. Trailing slashes are ours to add.** Django's `APPEND_SLASH` answers a slash-less path with a
301, and the client is deliberately configured with `follow_redirects=False` so that a redirect can
never quietly move a write to another host. The adapter therefore emits the trailing slash itself.

**3. One `LinkChange` per CMS call, applied in order, compensated on failure.** Their apply
endpoint carries exactly one link. Our deployment service batches the plans that share a source
page — 105 pages carry two plans and 8 carry three, out of 794 — and treats a deployment as
all-or-nothing with a single rollback handle.

The adapter preserves that: it applies the changes in sequence, re-reading the field's `version`
between them (the first write invalidates the lock the second was holding), and if any change
fails it rolls back those already applied, in reverse, before raising. The composite
`deployment_id` is the per-change ids joined by `,`; `rollback` splits it and unwinds in reverse.

A compensating rollback that itself fails leaves the field partly changed. That outcome is
reported as `CMSUnavailableError`, which the deployment service already treats as an unknown
write: the deployment stays `applying` and reconciliation resolves it against the live page.
This is strictly weaker than a transaction, and it is the honest limit of a single-link API.

**4. `occurrence` is field-scoped and 1-based.** For this provider, `LinkLocator.occurrence_index`
is read as the 0-based occurrence of the anchor *within the field*, and `+1` is sent. Computing
that index from our page-level locator is the subject of phase 2 (below) and is NOT done here.

**5. `href` is sent as a site-relative path.** Their example target is
`/industry-reports/qatar-…-market`. An absolute URL whose host we recognise is reduced to its
path; any other absolute URL is refused rather than sent, because a link that leaves the site is
not something this system is allowed to insert.

**6. `actor` is recorded on our side only.** Their payload has nowhere to put it. Our audit trail
is unaffected; theirs cannot answer "who approved this". Raised with the team, not blocking.

**7. Writes stay disabled.** `KEN_CMS_WRITES_ENABLED=false` until the owner turns them on, and the
write key is not even read while it is false.

## Phase 2, deliberately not in this ADR

The adapter can address a field; it cannot yet work out *which* field a plan's anchor lives in.
Our corpus was ingested as whole rendered pages, and a plan's `block_id` (`b306`) indexes our
parse of the page, not their field. Resolving it needs a deploy-time step that reads the report's
fields, finds the one containing the plan's recorded context, and computes the field-level
occurrence. The context is already stored — `link_plans.preview` holds the text before, at and
after the anchor — so this needs no contract change, only a new step in the deployment service.

It is a separate phase because it cannot be verified without the read key, and because guessing
at their response shapes would produce code that looks finished and is not.

## Consequences

- Connecting to a real CMS is an ASK action, and remains one. This ADR authorises the *code*, not
  the connection.
- Two adapters now implement `CMSProvider` over HTTP. The duplication is intentional: collapsing
  them into one adapter with a dialect switch would put a conditional on the single most
  safety-critical path in the system.
- The handover document in `docs/handover/` is now a historical record of what we asked for, not a
  description of what exists. Their guide is the live specification.

## Open questions, blocking the first write

These are asked of the technology team and are not answerable from the document:

1. **What happens when the same `operation_id` is sent twice?** Our constraint 12 requires every
   mutation to be idempotent. Until this is answered, a network timeout on an apply is an
   unresolvable ambiguity, and the adapter must never retry a write automatically. It does not.
2. Does `APPEND_SLASH` redirect, and can it be disabled for these endpoints?
3. Does the 409 body carry the current version, so we can re-plan without a second read?
4. Does an insert write a draft or go live? The October handover recorded a draft/publish
   workflow; their step 7 reads the change back immediately, which suggests it is live.
5. Does saving reformat or sanitise the HTML? We insert one `<a>` and change no other byte.
6. Is `occurrence` counted within the field or the whole report?
7. The full response schemas for validate, apply and rollback.
8. Is `thekenconsulting.com` the same content as `kenresearch.com`, with identical slugs? Every
   plan we hold addresses a `kenresearch.com` URL.
