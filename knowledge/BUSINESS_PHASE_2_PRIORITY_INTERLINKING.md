# Business Phase 2 — Priority Interlinking and Engagement Knowledge Base

> **Owner amendment — 2026-11-20:** Market Overview and Related Reports are hard placement
> exclusions. The existing module recommendations in sections 5.6 and 7 are retained only as
> historical planning context and must not be implemented. Regional siblings must earn a highly
> contextual placement in allowed explanatory prose or receive no link. Engagement and clear
> information gain are gates; priority and broad topical similarity cannot compensate.

- **Status:** approved product direction, 2026-11-19
- **Applies to:** kenresearch.com
- **Purpose:** make the next implementation phase build on the agreed SEO and business-engagement
  strategy instead of treating internal linking as a keyword-only exercise
- **Terminology:** this is *Business Phase 2*, not the already-completed repository software Phase 2
- **Production safety:** this document authorises planning and local implementation only. It does
  not authorise production CMS writes, policy promotion, live deployment, paid APIs, or changes to
  the repository's permission model.

## 1. Executive decision

Ken Research will use an adapted **Priority Silo** model.

The system must connect useful editorial content to the next action that helps the reader and the
business:

```text
Search or direct visit
    -> useful article, interview, POV, survey insight, or report
    -> topic hub or closely related supporting content
    -> relevant report, survey, expert, or consulting service
    -> sample download, analyst conversation, or qualified enquiry
```

The goal is not to maximise the raw number of links. The goal is to create a sensible journey from
information to a commercial or relationship-building next step while preserving topical relevance,
editorial quality, and user trust.

## 2. Business context

Ken Research has a large catalogue spanning reports, articles, blogs, perspectives, expert
interviews, surveys, consulting services, countries, industries, and sub-industries. The main
opportunity is not more disconnected content. It is stronger connections between these assets.

The website should behave as one connected decision-support system rather than separate libraries
for reports, publications, surveys, experts, and consulting.

The four principal commercial destinations are:

1. market-intelligence reports and sample downloads;
2. custom surveys and customer validation;
3. expert-panel conversations;
4. strategy consulting and qualified enquiries.

## 3. Principles retained from the source specification

Retain these ideas:

- organise content into clear topic clusters;
- use contextual links in editorial prose where they genuinely help;
- link child assets back to useful hubs or parent subjects;
- prevent dead ends on valuable entry pages;
- give important commercial pages stronger support from relevant content;
- avoid all-to-all linking and unrelated cross-category links;
- vary anchors naturally and avoid repeated keyword stuffing;
- keep link decisions explainable and auditable.

## 4. Rules that must not be implemented literally

The following source-document rules are too rigid for Ken Research:

- **Body links are not the only useful links.** Contextual links are important, but navigation,
  breadcrumbs, topic hubs, and carefully designed related-content modules also help people and
  search engines understand the site.
- **Do not enforce an 80/20 anchor formula.** Use concise, descriptive, natural anchors. Do not add
  generic phrases such as "click here" merely to satisfy a percentage.
- **Do not ban every cross-silo link.** Allow a cross-topic connection when it supports the same
  business question, such as EVs connecting to batteries and charging infrastructure.
- **Do not force every leaf page to link to a formal parent inside a paragraph.** Every valuable page
  needs a useful next step and should be discoverable, but the correct surface may be a breadcrumb,
  hub, inline link, or module.
- **Do not promote a page only because it is marked important.** Priority is a ranking input, never a
  licence to override relevance, geography, page eligibility, editorial safety, or trust controls.

## 5. Required Business Phase 2 capabilities

### 5.1 Priority-page registry

Provide a governed way to label pages with one or more business priorities:

- high-priority report;
- high-revenue report;
- consulting priority;
- survey priority;
- expert-panel priority;
- country or region priority;
- trending market;
- new or refreshed report;
- lead-generation page.

Minimum data per designation:

- page identity;
- priority type;
- priority weight or tier;
- reason;
- owner;
- effective and expiry dates where applicable;
- active/inactive state;
- created and updated audit information.

Priority must influence ranking only after the existing relevance, geography, page-family,
eligibility, and safety gates pass. A high-priority healthcare page must never receive irrelevant
links from unrelated content.

### 5.2 Topic hubs

Support curated topic hubs for commercially important combinations such as:

- India Electric Vehicles;
- Saudi Arabia Healthcare;
- UAE Logistics;
- Indonesia Digital Payments;
- Malaysia Industrial Packaging.

A hub should be able to present:

- a short market overview;
- key market indicators;
- priority and recent reports;
- relevant articles, POVs, and interviews;
- related surveys;
- consulting support;
- relevant expert routes;
- one clear next action.

All hub membership must be explainable. A hub is a curated decision page, not an automatically
generated doorway page or a list of every matching URL.

### 5.3 Cross-product journey graph

Model useful connections between Ken product families:

```text
article / POV / interview / blog
    -> topic hub
    -> report / benchmark / survey
    -> consulting / expert panel
    -> conversion
```

Required supported relationships include:

- article -> report;
- article -> survey;
- article -> consulting service;
- article -> expert route;
- report -> supporting insight;
- report -> custom research or consulting;
- report -> relevant expert;
- consulting page -> evidence, case study, or report;
- expert interview -> related report or service;
- survey result -> survey service;
- any eligible child asset -> its topic hub.

This requirement should extend the existing page-family routing policy rather than bypass it.
Any material policy revision requires a new version and the repository's existing governance path.

### 5.4 Next-best-action recommendations

Recommendations must consider the reader's likely intent, not show the same CTA everywhere.

Examples:

- market statistics -> "Download the complete market sample";
- competitor discussion -> "View competitor benchmarking";
- customer-behaviour insight -> "Discuss a custom customer survey";
- market-entry discussion -> "Speak with a market-entry consultant";
- expert interview -> "Connect with an industry expert".

Each recommendation must record:

- source page;
- destination or action;
- intent class;
- reason codes;
- placement surface;
- policy and scoring versions;
- confidence or deterministic score components.

### 5.5 Recommendation ranking

The initial product hypothesis is:

| Signal | Initial influence |
|---|---:|
| Same exact topic or governed market relationship | 30% |
| Same industry or sub-industry | 20% |
| Compatible country or region | 20% |
| Matching reader intent | 15% |
| Governed business priority | 10% |
| Freshness | 5% |

These are starting hypotheses, not permanent magic numbers. Preserve the repository's hard gates
before scoring. Record score components, evaluate on labelled examples, shadow the change, and
version any promoted policy.

Market adjacency must use governed typed relationships such as `component_of`, `complement_of`,
`upstream`, `downstream`, `substitute_of`, and parent/child or segment relationships. Do not infer
millions of relationships at request time. The existing market-graph proposal in `NEXT_STEPS.md`
item 32 is the starting point.

### 5.6 Placement surfaces

Support more than inline prose:

1. contextual body link;
2. topic-hub membership;
3. related reports by country or region;
4. adjacent market research;
5. related articles and trends;
6. relevant surveys;
7. consulting support;
8. sector-expert route;
9. "Choose your next step" module.

Use inline placement only in genuine editorial prose and continue to respect the existing placement
exclusions. Never place links in tables of contents, navigation labels, headings, tables, hidden
content, methodology boilerplate, or non-prose blocks.

The existing related-report module implementation should be extended, not rebuilt from scratch.

### 5.7 Two-way connection checks

When one page links to another, evaluate whether a useful return journey exists. Do not automatically
force reciprocal links. A return edge should be proposed only when it helps a reader and passes the
same relevance and safety rules.

### 5.8 Link and anchor quality

Use natural, descriptive anchors. Track anchor history per target and warn about:

- excessive repetition;
- generic wording such as "read more" or "click here";
- forced exact-match wording;
- anchors that omit necessary geography;
- overly long anchors;
- multiple links crowded into one sentence or paragraph.

Exact-match wording is allowed when it is the clearest natural description. There is no fixed anchor
percentage requirement.

### 5.9 Freshness and lifecycle

When a report or page is published, refreshed, redirected, superseded, expired, or removed, identify
affected:

- older related articles;
- topic hubs;
- regional and adjacent reports;
- service pages;
- expert interviews;
- high-traffic entry pages;
- existing recommendations and deployed links.

Recommend updates; do not silently rewrite production content. Preserve version checks, audit trails,
idempotency, verification, and rollback.

### 5.10 Quality and opportunity audits

Detect and prioritise:

- orphaned valuable pages;
- entry pages with no useful next step;
- report pages with no supporting insight or commercial route;
- hubs missing expected asset types;
- broken or redirected destinations;
- links to empty filters, temporary URLs, or non-canonical variants;
- excessive or irrelevant links;
- stale links to superseded reports;
- priority pages that remain too deep or under-supported;
- contradictory trust claims or broken counters on destination pages.

Use severity levels: critical, high opportunity, medium, and low.

### 5.11 Editorial controls

Reviewers need controls to:

- approve or reject a recommendation;
- choose inline versus module placement;
- move a recommendation to an appropriate section;
- lock a good existing link;
- replace an outdated target;
- suppress an inappropriate source-target pair;
- see why a recommendation was made;
- see the expected user journey and business destination.

Agents may propose, but promotion and production mutation remain governed by the repository's
existing controls.

## 6. Measurement

Measure business movement, not link volume alone.

Every measurable internal-link interaction should be attributable, where technically and legally
permitted, to:

- source and destination page;
- link or module item identity;
- placement surface and position;
- anchor text or CTA copy;
- topic cluster and page family;
- intent and priority classification;
- recommendation, policy, and experiment versions;
- resulting conversion event.

Primary outcomes:

- article-to-report click-through rate;
- report sample downloads;
- talk-to-analyst submissions;
- consulting enquiries;
- survey enquiries;
- expert-panel enquiries;
- pages per engaged session;
- assisted conversions;
- organic impressions and clicks for priority pages;
- orphan and under-linked page reduction.

Guardrail outcomes:

- broken-link rate;
- irrelevant-link rejection rate;
- anchor repetition;
- destination trust defects;
- reviewer override rate;
- rollback or verification-failure rate.

Do not claim causation from ordinary before/after analytics. Use the repository's existing experiment
and holdout governance when causal measurement is required.

## 7. Recommended implementation sequence

### Phase 2A — foundation and gap alignment

1. Map these requirements to existing services, contracts, policies, UI, analytics, and open items.
2. Produce an ADR only where a material architecture decision is actually needed.
3. Add the governed priority-page data model and read paths.
4. Define the first versioned page-family routing expansion.
5. Define topic-hub membership and explainability contracts.
6. Build tests and evaluation fixtures before changing ranking.

### Phase 2B — recommendations and surfaces

1. Add priority as a post-gate ranking signal.
2. Implement or extend topic-hub and next-best-action recommendations.
3. Extend existing related modules to cross-product groups.
4. Add freshness and opportunity audits.
5. Expose explanations and editorial controls in the review UI.

### Phase 2C — pilot and measurement

Run in shadow mode on five high-value clusters selected from business value, search demand, content
depth, and available commercial routes. For each cluster:

- one hub;
- approximately 20 strong entry articles or insights;
- priority reports;
- matching survey, expert, and consulting destinations;
- tracked contextual and module recommendations.

Observe for 6–8 weeks after any separately approved deployment. Compare engagement and conversion
outcomes with guardrails, then version and promote only the patterns that perform safely.

## 8. Acceptance criteria for the next build phase

The next build phase is complete only when:

1. priority designations are governed, auditable, time-bound where needed, and cannot bypass gates;
2. a reviewer can see why a page, hub, destination, and next action were recommended;
3. page-family journeys cover reports, insights, surveys, experts, consulting, and hubs according to
   an explicit versioned policy;
4. recommendations support both inline and module surfaces without violating placement rules;
5. anchors remain descriptive and natural without a fixed percentage formula;
6. freshness and orphan/dead-end audits produce actionable findings;
7. analytics can distinguish surface, intent, priority, and resulting business action;
8. fixtures cover accepted, rejected, cross-topic, cross-geography, stale, protected, and priority
   cases;
9. shadow-mode results are documented for the pilot clusters;
10. no production write, policy promotion, or live experiment occurs without the existing explicit
    approvals.

## 9. Explicit non-goals

- maximising link count;
- linking every page to every related-looking page;
- bypassing geography, relevance, editorial, or safety gates for revenue;
- auto-generating thin SEO doorway hubs;
- forcing generic anchors to satisfy a ratio;
- introducing mandatory paid APIs or managed services;
- changing production content as part of implementing this knowledge base;
- treating correlation as proof of SEO or revenue causation.

## 10. Relationship to current repository work

Build on these existing foundations instead of duplicating them:

- content inventory and page identity;
- taxonomy and geography gates;
- recommendation plans and reason codes;
- anchor and placement safety;
- related-report modules;
- review controls;
- analytics attribution;
- experiments, learning, autonomy, and rollback controls.

Pay particular attention to the open work already documented in `NEXT_STEPS.md`:

- item 29 — page-family routing;
- item 31 — related-report modules and CMS surface;
- item 32 — governed market adjacency graph;
- item 34 — inert evaluation corpus under the current gates.

The first engineering task is a gap analysis against those foundations. Do not begin by inventing a
parallel recommendation engine.
