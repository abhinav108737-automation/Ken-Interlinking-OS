# Interlinking optimisation lessons

Durable rules learned from the Ken Consulting v0.2 live tests. These rules supplement the
governing ADRs and never bypass approval, version, rollback or protected-page controls.

## 1. Verify what readers see, not only what the CMS stores

A link can exist in a CMS field and in the public page's serialized data but still be invisible.
The `v02_growth_drivers` field contains headings, description paragraphs and bullet lists; the
frontend can render only part of that structure. A successful CMS apply is therefore not enough.

For every live link:

1. verify the CMS field and version;
2. revalidate the public page cache;
3. verify one real rendered `<a>` with the intended `href` and anchor text;
4. reject a placement that exists only in hidden or serialized content.

## 2. Prefer visible explanatory bullets over hidden descriptions

On the Procurement Spend Control report, `document management` was first linked in a Market
Opportunities description. The CMS stored it, but the public component displayed the bullet list
and omitted that description. The repair moved the phrase into an existing visible bullet:

> Separate registration and renewal workflows, reviewed in 2026, create recurring **document
> management** and data-maintenance needs; configuration packages can reduce repetitive
> administrative work.

The destination is the Kuwait Cloud-Based Document Management Systems report. The wording is a
small local clarification supported by supplier registration and document-maintenance context.

## 3. A contextual copy edit is a separate governed operation

When the needed anchor is absent from visible copy, use the content-change API only after a dry
run. Store the exact `find` and `new_text`, expected version, deployment ID and rollback handle.
Do not silently loosen the normal verbatim-anchor rule.

Observed Ken Consulting contract on 2026-11-19:

- dry run: `POST /internal/pages/{slug}:{field}/content-changes/validate/`;
- apply: `POST /internal/pages/{slug}:{field}/content-changes/`;
- replace payload: `change = {"op":"replace", "find":"...", "new_text":"..."}`;
- insert payload: `change = {"op":"insert_after", "after_text":"...", "sentence":"..."}`;
- allowed markup in new copy: plain text and `<a href="...">` links;
- apply requires `operation_id` and `expected_version`;
- rollback uses the normal deployment rollback endpoint;
- after every apply, read the new version before another change.

## 4. Use one atomic copy-and-link change when possible

If the CMS permits an anchor in `new_text`, create the contextual words and link in the same
version-locked deployment. This avoids an intermediate unlinked state and prevents a second link
operation from targeting the wrong occurrence.

## 5. Failed attempts must compensate and remain visible in the record

The first repair attempt used an assumed payload and was refused with HTTP 422. The script restored
the original hidden link as deployment 23. After Snehal supplied the contract, deployment 23 was
rolled back and the validated atomic repair was applied as deployment 24. Production failures are
evidence: record them, restore the prior state, and update the contract knowledge before retrying.

## 6. Preview one useful destination fact when it improves the reader's decision

The first Kuwait topic canary was accurate but described only the destination capability. Owner
feedback asked for a concrete preview of what the reader would gain by clicking. The revised link
kept the same Market Trajectory placement and added one destination-supported figure:

> For execution, **order management systems** help turn inventory forecasts into stock
> reservations and fulfilment plans. The Kuwait market is projected to reach USD 137 Mn by 2032.

Use this pattern only when the destination fact is publicly visible, current, directly relevant to
the source discussion and recorded verbatim as evidence. Prefer one figure, not a string of metrics.
Keep the capability connection in the sentence, avoid unsupported causal language and ensure the
sentence remains useful without the link. This improves information scent and may improve qualified
click interest; it does not increase the authority-routing score or guarantee clicks or rankings.

Owner feedback also rejected em dashes as an AI-writing signal. Contextual drafts should prefer
short sentences, normal commas and full stops. Punctuation must not make otherwise accurate copy
look machine-generated.
