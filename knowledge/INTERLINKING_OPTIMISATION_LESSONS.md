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

## 7. The rationale is not the copy

Twelve exact-market cluster links went live on 2026-11-20 with sentences that were the draft's
internal `reader_value` note verbatim: the explanation of *why* a link helps, published as page
text. Eleven ended "giving readers a ... benchmark". Every technical gate passed, because none can
judge voice. The owner asked for all twelve to be rewritten, and they were (deployments 55-66).

**The owner's copy rule for an inserted bridge sentence (2026-11-20):** at least two sentences,
32-38 words in total, no em or en dashes, no address to the reader ("readers", "you"), currency
figures rounded to at most one decimal, and never identical to the draft's rationale field. Every
fact must come from the destination report's recorded evidence, and no sentence may assert a cause
that the destination report does not itself state.

The rule lives in one place, `tools/v02-contextual-audit/copy-rules.mjs`. Every applier checks it
before any CMS request, so it holds whichever builder or model drafted the copy, and the review
dashboard shows the same result on each card before anyone approves it. A rule that only a builder
follows is a rule the next builder will not.

This supersedes the 20-word limit (and its 21-35 word extension) in lesson 6 for inserted bridge
sentences. The six Kuwait topic links already live (deployments 35-42) predate it and run 25-28
words; they were left as approved.

## 8. Most of a report is invisible to its readers

A CMS write can succeed while the public template never shows that section. Mapping all v0.2
fields of three reports against their public pages (`map-rendered-sections.mjs`) found that only
**Market Trajectory** and **Key Stakeholders** render in full on those three. Growth Drivers shows
1 of its 10 paragraphs, Research Methodology and FAQ show none, and Regional Analysis showed none
on these three reports.

Visibility is decided paragraph by paragraph, not section by section. Seven links outside Market
Trajectory are live and visible, in Market Segmentation, Regional Analysis and Growth Drivers,
because their exact paragraph is one the template shows. *Correction, 2026-11-25:* an earlier
version of this lesson said every link placed outside Market Trajectory had vanished or been
rolled back. That generalised from three reports and was wrong.

Map the rendered paragraphs of a report before drafting for it, and choose placements only from
paragraphs a visitor can see. This is a property of the public template, not of the content, so it
is worth raising with the site team: much of the written analysis never reaches a reader.
