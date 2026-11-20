# Full contextual interlinking

## Business goal

Internal links should help a reader continue their research. The wording around a link must explain
what the destination adds, while preserving the source report's market, facts, tone and meaning.

The goal is not to insert more keywords. The goal is to make the next useful piece of information
easy to reach.

## The reader test

Approve a contextual link only when a reader can answer both questions from the surrounding copy:

1. **Why is this destination related to what I am reading now?**
2. **What additional information will I get if I click?**

If either answer is unclear, do not create an inline link.

## Four permitted outcomes

### 1. Link useful text that already exists

Use an existing phrase when it clearly describes the destination. This is the lowest-risk option
and does not change the report's wording.

### 2. Make a small contextual edit

Add only the missing market term or qualifier when the sentence already supports it. The edit must
not change the sentence's claim.

Example:

> “create recurring data-maintenance needs”

becomes:

> “create recurring **document management** and data-maintenance needs”

### 3. Add one short bridge sentence

Use this only when the source discusses the same need but does not name the destination concept.
The sentence should explain the relationship, not advertise the report.

Example pattern:

> “These supplier records also create demand for **document management systems** that control
> approvals, renewal documents and access.”

The statement must be supported by source or target evidence. No invented facts, numbers or market
claims are allowed.

### 4. Create no link when explanatory context is missing

Do not use the Related Reports area as a fallback interlinking surface. The website already lists
related reports there, so adding another recommendation is duplicative and feels pushy. If the
relationship cannot earn a natural placement in allowed explanatory prose, create no link. A
missing link is better than a misleading or engagement-poor link.

## Relevance routes

At least one route must be true:

- exact same market in another geography;
- parent market to a clear submarket, or the reverse;
- one solution is a direct input, control or next step in the same workflow;
- the destination answers a question raised in the source paragraph;
- the same buyer would naturally need both capabilities for the stated use case.

Sharing only a broad technology word such as “AI”, “cloud”, “analytics” or “platform” is not enough.

## Copy rules

- Preserve every existing number, date, entity, market claim and comparison.
- Default to 20 or fewer changed/added words.
- Edits over 20 words follow ADR-0023: maximum 35 words and at least one quantitative fact copied
  from a stored evidence span.
- One copy edit and one new inline link per block.
- Use neutral research language. No urgency, hype, curiosity bait or “click here”.
- Anchor text must accurately predict the destination.
- Do not add a geography that the evidence does not support.
- Never change legal, methodology, scope or disclaimer copy for interlinking.

## Placement rules

Preferred visible sections:

1. growth drivers and opportunities;
2. market trajectory;
3. segmentation explanatory prose;
4. regional analysis.

**Hard exclusions approved on 2026-11-20:**

- **Market Overview:** it must remain entirely focused on the report's stated market. Do not add,
  edit or manipulate its copy for an interlink to another market.
- **Related Reports:** it already performs the related-report listing job. Do not place inline
  links there and do not generate a second related-report module.

Allowed for manually reviewed contextual copy: a visible list item that is a complete explanatory
sentence. This does not loosen ADR-0029's deterministic existing-text placement rule.

Never place links in navigation, table of contents, headings, table cells, hidden descriptions,
source citations, methodology or boilerplate FAQs.

## Evidence and review record

Every proposal must show:

- source and destination URLs;
- source field and visible placement;
- exact `find` text;
- before and after copy;
- anchor text and destination;
- source/target evidence supporting the relationship;
- a plain-language reason the reader may want to click;
- CMS validation result and expected version.

## Publishing and learning

Use the content API as one atomic copy-and-link change wherever possible. Dry-run first, apply only
the approved preview, verify the CMS response, revalidate the public page and confirm one rendered
anchor. Record the deployment ID and rollback handle.

Feedback must use structured reasons: accurate/useful, inaccurate, too promotional, off-topic,
awkward wording, wrong placement or wrong destination. Learning may propose future rules, but it
does not grant itself permission to publish.

## Reviewer and drafter workflow (published 2026-11-20)

The reviewer and drafter must use current CMS evidence, not only a stored recommendation:

1. read the current source field and destination fields without changing them;
2. refuse Market Overview, Related Reports, tables, headings, citations and boilerplate;
3. require the anchor inside visible explanatory paragraph or complete explanatory list-item copy;
4. show a destination passage that supports the same need or workflow;
5. prefer the existing sentence unchanged when it already passes the reader test;
6. draft a small edit only when evidence supports every added claim; otherwise create no link; and
7. record exact find text, before/after copy, expected source version, evidence and verdict.

A shadow result is not CMS approval or publication. Live changes still require dry-run validation,
explicit approval, an atomic version-locked apply, rendered-page verification and a rollback record.

## Engagement and information-gain gate

Relationship alone is not enough. Before scoring or publishing, a proposal must pass all four:

1. the destination is strongly related to the exact point being discussed;
2. the surrounding sentence tells the reader what additional information the destination adds;
3. the anchor clearly predicts the destination market and is not a vague fragment;
4. the sentence reads naturally even when the hyperlink is removed.

If a proposal merely mentions another capability to manufacture a link, it is pushy and must be
rejected. Bot crawlability is a result of coherent, descriptive connections; it is not permission
to create more links.

## Contextual editorial scorer reference (published 2026-11-20)

The review dashboard displays a 100-point editorial reference alongside each evidence-backed
draft. It does not claim to reproduce a Google ranking system, predict an AI citation or replace
the evidence gates.

| KPI | Weight | Editorial question |
|---|---:|---|
| Reader need | 15 | Does the source create a genuine question, task or useful next step? |
| Source evidence | 15 | Does the exact source passage establish that need? |
| Destination fit | 20 | Does destination evidence answer or materially advance it? |
| Anchor expectation | 15 | Does the anchor accurately predict what the reader will open? |
| Context and placement | 15 | Is the link natural in an approved explanatory section? |
| Added information value | 10 | Does the destination add information instead of repeating the source? |
| Quality and risk | 10 | Is the wording supported, neutral, non-redundant and non-promotional? |

Reference outcomes are 85–100 `approve`, 70–84 `revise` and 0–69 `reject`. Missing source or
destination evidence produces `insufficient evidence`, regardless of the arithmetic total.

The score cannot bypass these independent gates: the source phrase exists; destination evidence
exists; placement is in an approved visible section; the inline link is not duplicated; and the
published result is a visible, crawlable `<a href>`. The rendered-link gate remains pending until
an approved change is applied and verified on the public page.

## Exact-market regional clusters (published 2026-11-20)

Two reports with the same normalized market and different geographies form a strong cluster, but
cluster membership alone does not authorize a link. Each direction must independently provide a
publicly visible explanatory placement and a factual comparison that helps the reader understand
scale, growth, demand or operating differences.

Use the full destination market name as the anchor. Prefer a short comparison after a public
Market Trajectory or Regional Analysis paragraph. Support every number or claim with evidence from
the corresponding report. Do not use Market Overview or Related Reports.

A cluster may be bidirectional when both pages pass these checks. If only one page has a visible,
useful placement, keep the relationship one-way. Do not manufacture the reverse link for graph
symmetry. CMS presence is insufficient; eligibility requires public rendered visibility.
