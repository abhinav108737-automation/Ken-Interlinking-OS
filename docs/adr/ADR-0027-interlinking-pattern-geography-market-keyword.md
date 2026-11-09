# ADR-0027 — The interlinking pattern: reader geography, market identity, and a keyword anchor

- **Status:** Stage A implemented and verified on the full corpus, 2026-10-31. Stage B not started.
- **Decision owner:** the project owner, from a review of real engine output on 2026-10-30, with the
  §4 containment exemption and the §5 correction added 2026-10-31.
- **Amends:** ADR-0011 (recommendation engine) §4–§5: geography and market move from *score
  contributions* to *gates*, and the anchor is no longer any natural phrase but the target's market
  keyword. **Resolves:** NEXT_STEPS item 9 (self-describing anchors).
- **Depends on:** [ADR-0023](ADR-0023-contextual-copy-with-links.md) for the part of this pattern that
  cannot be satisfied by marking up existing text.
- **Authority:** CLAUDE.md — a change to verified Phase 3 behaviour is an architecture change and
  needs a record.

## Context: what the owner rejected, and why

The owner reviewed the engine's ten highest-confidence plans on the real corpus. Two were good, one
acceptable, seven poor. Two rejections, in the owner's words:

1. **Indonesia Natural Personal Care Market → India Cosmetics Market**, anchor `cosmetics`.
   "A user reading an Indonesia natural personal care report might not be interested in India's
   market because he's trying to penetrate into Indonesia market." And: a bare `cosmetics` anchor
   does not rank; `cosmetics market` would.
2. **North America Nut and Seeds Market → Asia Pacific Vegetable Seed Market**, anchor `seed`.
   "Completely a different market", the regions differ, and the anchor is a word rather than the
   market keyword.

This is not a handful of bad rows. Measured over the 126 plans on the 498-page sample, **112 (89%)
cross a primary geography boundary**.

### Root causes, found in the code and data

- **Geography was advisory.** `_geography_score` in `scoring.py` computes `same_country`,
  `region_contains_source`, `same_region` and `global_target` and turns them into a *score*. A
  mismatch lowered a number instead of refusing the pair.
- **`same_country` counted incidental mentions.** It is true when the two pages share *any* country
  named anywhere in their text. The Africa Cashew report mentions India, so "India Dry Fruits →
  Africa Cashew" scored as same-country.
- **The anchor keyword is usually absent.** `cosmetics market` occurs **0 times** in the Indonesia
  page; `seed market` occurs **0 times** in the nut page (which uses `seed` 40 times, always in the
  edible sense). The engine can only mark up text that already exists, so the owner's anchor rule is
  **unreachable** without the sentence rewriting of ADR-0023. The two instructions are one
  instruction.

## Decision

### 1. Geography is a gate, on the *primary* geography

A page's primary geography is the highest-scored entry in `page_analyses.geographies`, ties broken
deterministically. Incidental mentions stop qualifying as a match.

| source → target | decision |
|---|---|
| same country | **allow** |
| country → a region containing it (taxonomy ancestry) | **allow** |
| region → a country inside it | **allow** |
| any → a global target | **allow** |
| different countries, same region | **allow only if** the source text establishes the relationship (export, supply chain, a named comparison) |
| different regions, **same market** | **allow** — a regional sibling; see §1b. The anchor must name the target's geography. |
| different regions, different markets | **refuse** |
| global source → one country | **allow only if** the source text names that country |

### 1b. Regional siblings are the FIRST priority, not a refusal (owner, 2026-11-01)

§1's bottom row — "different regions: refuse" — was **wrong**, and it was my over-reading of the
owner's original rejections. Both of those pairs (Indonesia natural personal care → India cosmetics;
North America nut and seeds → Asia Pacific vegetable seed) crossed a **market** boundary as well as a
geographic one. The owner never objected to the same market elsewhere. On 2026-11-01 they stated the
opposite priority, in their own example:

> "We have global EV market report, we have India EV market report, we have Saudi Arabia EV market
> report, we have GCC EV market report … these are all regional reports. So we must find a way … to
> interlink all any regional reports, that is one priority."

**The rule.** When the two pages are the **same market** — their market cores match, per
`placement.market_core` — a differing geography is `REGIONAL_SIBLING` and is **allowed**, with no
requirement that the source text mention the target's place. The only protection is §4: the anchor
*must* name the target's geography. That is what keeps the owner's Brazil case impossible — a page
reading "Brazil Niche Fragrance Market" may link to the global report only as "global fragrance
market", never as bare "fragrance market".

A cross-region pair with **different** market cores is still refused, so the original two rejections
remain impossible.

**What the old rule cost.** On the full corpus: 962 market cores span more than one geography,
covering 3,186 pages (36% of the inventory), and **6,117 same-market pairs were refused as
`geography_mismatch`** — seven times the 853 plans the entire run produced. Among reports alone, 372
cores carry three or more geographies (`telecom` 14, `car rental` 14, `coffee` 13, `pet food` 13).

**Ordering matters, and getting it wrong was measurable.** The sibling test must run *before* the
same-region conditional row. Placed after it, two countries of one region fell into
`CROSS_GEOGRAPHY_MENTIONED`, which demands the source text name the target: 28 of 132 sibling pairs on
a 10-source probe were refused because a Malaysia car-rental report has no reason to write
"Indonesia". With the order corrected, geography refusals among sibling pairs fell to **zero**.

### 1c. Stage A cannot deliver Priority 1: the anchor is the binding constraint

With the gate relaxed, the same probe shows where sibling links actually die:

| Of 132 regional-sibling candidate pairs | |
|---|---|
| refused by the geography gate | **0** (was 28) |
| **refused: no verbatim geography-qualified anchor** | **104 (79%)** |
| plans created | 4 |

This is a **structural** limit, not a tuning problem. §4 obliges the anchor to name the target's
geography, and §3 allows only text already present; a Malaysia car-rental report does not contain the
phrase "Indonesia car rental market", and there is no reason it ever would. **Inline contextual
anchors are the wrong surface for regional siblings.**

The right surface is a **related-reports module**, where the link text is the target's own title and
therefore names its geography by construction. Note the irony: the "KSA Modular Kitchen **Furniture
Market**" defect of §5 was an inline anchor placed *inside* such a module. The module is the correct
home for Priority 1 and Priority 2 links; the defect was putting a contextual anchor in it. This is
recorded as a new capability, not a fix to Stage A — see NEXT_STEPS.

### 2. Market identity is a gate

Refused unless the pair is the same market, a taxonomy parent/child, or a declared supply-chain or
complementary relation. **Word overlap never qualifies** — that is what made edible `seed` match
planting `seed`.

### 3. The anchor is the target's market keyword

The anchor is the target's own market phrase plus `market` — `cosmetics market`, `skincare market`,
`contract logistics market`. It is derived from the **target's own identity** (its slug with
geography prefixes stripped and the trailing `-market` removed, or its title), never from the broad
`market_id` that analysis assigns: that yields generic parents and produced anchors such as
`food market` pointing at a vending-machine report.

### 4. A cross-geography link must say so

Whenever the target's geography differs from the source's — the conditional rows of §1 — the anchor
must also name it: `India cosmetics market`, never bare `cosmetics`. A cross-geography link can then
never be disguised as a local one.

**Containment is exempt (owner decision, 2026-10-31).** As first written this section was ambiguous:
the opening clause reads "whenever the target's geography differs", which would catch the
*unconditional* allow rows of §1 as well, while the parenthetical names only the conditional rows.
The owner resolved it in favour of the parenthetical. A **country → a region containing it**, or a
**region → a country inside it**, may keep a bare keyword anchor, because the target genuinely serves
the reader: a GCC report covers the UAE.

The decision was taken on measured output, not in the abstract. Of 57 plans from the 208 `/articles/`
sources, 16 were country → containing-region with a bare anchor:

| anchor, as it reads in the source | source geography | target |
|---|---|---|
| `gold market` | United Arab Emirates | GCC |
| `CPaaS market` | India | Asia Pacific |
| `real estate market` | Saudi Arabia | Middle East |
| `used car market` | Indonesia | Southeast Asia |

The cost is accepted knowingly: a reader may click text that reads "the UAE **gold market**" and land
on a GCC report. The relief is that the target is a superset of the reader's geography, which is not
true of the sibling-country case (Indonesia → India) that prompted this ADR and which §1 still
refuses. The conditional rows — **different countries in one region**, and **global source → one
country** — continue to require the geography in the anchor.

This exemption needed no code: §4 had never been implemented, so the engine's behaviour already
matched the decision. What changed is that the ADR no longer contradicts the engine.

### 5. Two stages, because §3 is not always reachable

**Correction, 2026-10-31.** This section first claimed a **20%** verbatim-anchor yield, from a
300-page sample read by hand. That number was wrong, and the four examples offered to support it did
not survive checking — none of the four contained the anchor it was credited with. The figure is now
measured from engine output rather than estimated:

| Measurement | Result |
|---|---|
| Full corpus, all 8,858 sources: candidates evaluated | 706,912 |
| → plans with a verbatim market-keyword anchor | **853** (0.10% of candidates) |
| `/articles/` sources only: 208 sources, candidates | 16,623 |
| → plans | **57** (0.34% of candidates) |
| Plans per eligible source page, articles vs. whole corpus | 0.27 vs. 0.10 |

Yield is therefore an order of magnitude below the original estimate, and the earlier "~1,770 pages"
should be read as refuted, not merely revised. Two findings matter more than the headline:

- **Articles are the natural link source.** They are 2% of the corpus but produce plans at roughly
  2.7× the per-page rate of reports, and at the time of the first audit supplied nearly every
  anchor that survived editorial review. Reports are the natural *target*.
- **The gates, not the anchor rule, do most of the filtering.** Across the full corpus:
  `geography_mismatch` 257,088, `market_unrelated` 240,383, `page_type_incompatible` 166,576,
  `no_market_keyword_anchor` 39,435. The anchor rule removes about a twentieth of what geography
  and market identity remove between them.

- **Stage A (this ADR).** Deterministic. Gate on §1 and §2, anchor per §3–§4, verbatim only. No
  model, no text changes, every other byte of the page identical.
- **Stage B (ADR-0023).** Where no verbatim keyword exists — the overwhelming majority — mold the
  sentence so the keyword is present and carries a statistic from the target page. The owner
  authorised this on 2026-10-30: "I am allowing you to mold sentence in order to get the context
  clearly and make weightage to it", with market size and CAGR as the weight. Every number must
  trace to a verbatim evidence span on the target page.

Stage A ships first and stands alone. Stage B is a separate phase and does not gate it. But the
measured yield changes the balance between them: Stage A alone cannot cover the site, so Stage B is
the substance of this pattern rather than an enhancement to it.

### 6. Where the model belongs

The gates of §1–§2 are a lookup over the taxonomy tree and stored analyses. They stay **deterministic**:
reproducible, unit-testable, and explainable from the stored plan row (CLAUDE.md #8). The local model
(`qwen3:8b-q4_K_M`) is used for the judgement that genuinely needs it — drafting the Stage B sentence
and choosing which statistic earns the click — behind the Pydantic contract and provenance checks of
ADR-0023. No second agent framework is introduced; LangGraph remains the supervisor (TRD §2).

## Consequences

- Far fewer plans, and the owner's rejected patterns become impossible rather than merely unlikely.
  Verified: 706,912 candidates → 853 plans on the full corpus, with **zero** cross-region links,
  against 112 of 126 (89%) crossing a primary geography boundary before this ADR.
- New rejection counters make each refusal visible in the dashboard and in job counters.
- The small 498-page sample was itself part of the cause: within it an Indonesia page often had no
  Indonesian neighbour, so the engine reached across regions. The full inventory holds 337 Indonesia
  pages across 246 markets and 1,715 India pages across 591 markets, so the pattern must be run on
  the full corpus to be judged fairly.
- `evals/datasets/interlink-quality/` records the owner's labels so a future change is measured, not
  argued.
- **Routing is now the dominant editorial lever.** Because articles out-yield reports per page and
  supply the better anchors, "which page family may be a source" deserves to become explicit policy
  rather than a side effect of eligibility.
- **An inline anchor is now placed only in prose (`placement.block_is_prose`).** 185 of the 1,028
  plans (18%) anchored inside a cross-sell list entry — another report's title — so a France report
  linked to a Germany report through text reading "Thailand Residential **Furniture Market**". Two
  attempts were needed, and the first failed for an instructive reason:

  1. *Corpus page titles as `enclosing_phrases`* (`placement.corpus_title_phrases`). Correct in
     principle and kept, but it removed **0 of 17** on a 14-source probe: those cross-sell entries
     link to pages **outside the inventory**, because the sitemaps cover about 18% of the live site,
     so their titles are not ours to compare against.
  2. *An intrinsic test of the block.* A block hosts an inline anchor only if it carries
     sentence-terminal punctuation or runs longer than `MAX_LABEL_WORDS` (25). Punctuation alone was
     too blunt — it rejected long unpunctuated paragraphs — and length alone admits every short
     label. On the same probe: **17 plans → 0**, every one of them a defect.

  The rule does not exclude `li` as a block kind: a list item written as a sentence is still prose.
  It errs toward refusal by design, because a missed link costs a link while an anchor in another
  report's title misleads the reader.

- **The `anchor-selection` eval suite had stopped gating the engine, and this ADR exposed it.** The
  scorer drove `placement.anchor_phrases` (taxonomy aliases) while the pipeline had moved to
  `placement.market_keyword_anchors`. The suite stayed green throughout, because both paths agreed
  on the fixtures until the fragment guard was added. It now runs
  `geography_gate → market_keyword_anchors → find_placement`, the pipeline's own path, and the
  dataset is reissued as `core.v2.jsonl`. The lesson is general: an eval scorer that re-implements a
  decision instead of calling the production function will drift silently, and
  `ken_services/learning/shadow.py` and `scorers.py` have the same shape elsewhere (NEXT_STEPS).
  A new eval decision, `geography_refused`, keeps a §1 refusal from being scored as an anchor
  failure.

## Risks and limits

- **Yield is far lower than this ADR first claimed** — see the correction in §5. Stage A produces 853
  plans for 8,858 pages, roughly one link per ten pages. That is the intended direction, but it is
  not enough interlinking to matter commercially on its own, which is the strongest argument for
  Stage B.
- **The market gate is stricter than intended, because the taxonomy declares no relationships.**
  Taxonomy v3 carries 0 of 5,451 markets with an `adjacent` / `complementary` / `upstream` /
  `downstream` edge: the generator never emitted them. So `market_unrelated` (240,383 refusals) is
  currently enforcing "same market or taxonomy parent/child" only, and §2's "declared supply-chain or
  complementary relation" clause is unreachable. The sibling rule (similarity ≥ 0.78 for a
  `related_market` pair) is the stopgap; declaring real edges is the fix.
- **The site is larger than the inventory.** The sitemaps cover roughly 18% of the live site, so about
  40,000 live pages are absent from the corpus entirely. Both yield figures and geography coverage are
  therefore measured on a fraction of the real site.
- **Refusing all cross-region links may cost legitimate ones.** Ken sells cross-market research, and
  an export or supply-chain angle is real. §1's conditional rows are the relief valve; if they prove
  too narrow the owner can widen them, and the eval dataset will show the cost.
- **"Same geography" is not the same as "serves the reader".** The gate is a proxy for reader intent,
  and a proxy can be wrong in both directions.
- **Stage B carries editorial risk** that Stage A does not: it publishes new words under Ken
  Research's name. ADR-0023's provenance, tone-lint and human-approval rules are what contain it, and
  none of them is relaxed here.
