# ADR-0029 — Context anchors when the market keyword is absent

- **Status:** accepted and implemented 2026-11-19. Measured results in *Verified* below.
- **Decision owner:** the project owner, 2026-11-19, from measured engine output on two real
  Ken Consulting corpora. Geography boundary chosen by the owner the same day.
- **Amends:** [ADR-0027](ADR-0027-interlinking-pattern-geography-market-keyword.md) §3 (the
  anchor is the target's market keyword). The keyword stays first; this adds a **fallback**
  when it is absent. Geography and market gates are untouched.
- **Authority:** CLAUDE.md — a change to verified Phase 3 behaviour needs a record.

## Context: the rule had stopped being reachable

ADR-0027 made the anchor the target's *market keyword*. In practice the engine derives one
phrase per target, from the target's title minus its geography, and requires that phrase —
plus the word "market" — to occur in the source's prose.

Measured 2026-11-19 over two corpora exported live from the Ken Consulting CMS:

| Corpus | Pages | In-prose link plans proposed |
|---|---|---|
| 12 Saudi Arabia technology reports (v0.1) | 12 | **0** |
| 7 ASEAN / Gulf reports (v0.2) | 7 | **0** |

Of 132 candidate pairs in the Saudi corpus, 58 cleared both gates and then died at
`no_market_keyword_anchor`. Every report names only its *own* market: 12 of 12 and 6 of 7 name
a market with the word "market", and **none names another report's market**. Ken Research
reports are written self-contained.

A rule that cannot fire is not protecting anything. In the owner's words: *"if there is no
market keyword present on the report page where we want to interlink and we have the context
in any segment's content, then it is allowed to create the anchor text with context to create
interlinking, else we should never be able to create interlinking."*

### The root cause is the phrase, not the word "market"

Dropping the `" market"` suffix alone changes nothing — still 0 of 42 pairs on the v0.2
corpus. The engine derives the phrase from the **whole title**, and whole titles do not recur
in other reports' prose. The concepts inside them do:

```
derived phrase : "Electric Two-Wheeler Adoption Ecosystem"   occurs in 0 of 7 reports
sub-phrase     : "two-wheeler"                               occurs in 4 of 7 reports

derived phrase : "EV Charging Infrastructure and Smart Energy Mobility Ecosystem"   0 of 7
sub-phrase     : "charging infrastructure"                                          3 of 7
```

The decisive case is deployment 9, the one live link the team reviewed and deliberately kept.
Its anchor is `Qatar tire distribution market`. In the UAE source page:

```
"qatar tire distribution and retail service ecosystem market"  present = False   <- what the engine asks for
"qatar tire distribution market"                               present = True    <- the approved anchor
```

The engine could not propose the link a human approved, because it asked for the full title
rather than a meaningful part of it.

## Decision

### 1. The market keyword still comes first

Nothing about ADR-0027 §3 changes when the market-keyword anchor is available. The fallback is
tried **only** when no market-keyword anchor can be placed, so every existing plan keeps the
anchor it has today.

### 2. A context anchor is a sub-phrase of the target's market phrase

Admissible context anchors are contiguous word runs of the target's own market phrase, longest
first, that occur in the source's allowed prose. They are not free editorial phrases, not
synonyms and not model output: the anchor still names the target's subject, in the target's own
words.

A run that begins or ends on a stop word is rejected, so "and Fleet Management" and "Leasing
and" never become anchors. The existing `anchor_rule_ok` length and shape checks still apply,
as do the placement rules (editorial prose only; never a heading, bullet, table cell,
table of contents or left navigation panel).

### 3. Geography honesty is preserved — the owner's boundary

ADR-0027 §4 holds: when the target's geography is neither the source's own nor a container of
it, the link must be labelled. For a context anchor that is satisfied in **either** of two
ways:

1. the anchor text itself names the target's geography ("Qatar Tire Distribution"), exactly as
   a market-keyword anchor does today; **or**
2. the anchor's **own block** names the target's geography.

Anything looser was rejected. Specifically, "the geography is named somewhere on the page" was
considered and refused: a reader meeting "Commercial Vehicle" in a Philippines paragraph should
not land on Indonesia because the word "Indonesia" appears twenty-seven paragraphs earlier.
Dropping the geography requirement entirely was also refused — that is the case the owner
revoked the containment exemption over on 2026-11-12.

Measured on the v0.2 corpus (every page a different country, so these are a floor, not a
typical yield):

| Geography rule | Anchorable pairs of 42 |
|---|---|
| share a geography | 0 |
| **block names the target's geography** | **1** — and it is deployment 9, the approved link |
| named anywhere on the page | 3 — *rejected* |
| no requirement | 6 — *rejected* |

On a same-geography cluster the first row carries the yield, and that is the expected shape of
the 100-report run.

### 4. This does not resurrect pre-ADR-0027 anchors

The anchors the frontend team saw rendering in November — "two-wheeler", "charging
infrastructure", "fleet availability" — were chosen by hand to exercise rendering, and several
cross a geography boundary with no label. Under this decision the engine still would not
propose them unless their block names the target's geography.

## Consequences

- `no_market_keyword_anchor` stops being the dominant refusal; pairs that clear both gates now
  usually have an anchor.
- The reason code vocabulary is unchanged: a context anchor that cannot be placed still records
  `no_market_keyword_anchor`, because the proposition — *the target's subject is not named in
  this page's prose* — is the same one.
- Anchors become shorter on average. A short anchor carries less meaning, so the placement
  rules and the geography label do more of the work of telling the reader where they are going.
- Eval expectations that encode "this pair yields no placement" because the full title was
  absent may now yield one. Any such case is re-judged against this ADR, never silenced.

## Verified, 2026-11-19

Implementation: `market_subphrases` and the context tiers of `market_keyword_anchors`, plus
`_names_a_longer_entity` in `find_placement`. `allow_context=False` restores the old behaviour.

| Measurement | Before | After |
|---|---|---|
| Saudi v0.1 corpus (12 pages), plans proposed | 0 | **5** |
| Demo eval corpus, plans proposed | 0 (`assert plans` failed) | **7 sources** |
| Demo eval `precision_at_3_macro` (floor 0.90) | n/a — nothing to score | **1.00** |
| Demo eval `bad_examples` / `anchor_mismatches` | n/a | **[] / []** |
| Unit suite | 4,766 passing | **4,769 passing**, 18 new |

The demo eval still fails `recall_good_macro` (0.1833 against a 0.70 floor). That floor was
calibrated against pre-ADR-0027 behaviour and is NEXT_STEPS item 34's known-red case, deferred
by the owner (D4) until the market graph exists. It is recorded here, not silenced: the test
moved from proposing *nothing* to proposing a small, perfectly precise set.

### Two defects this change introduced, found by its own tests

1. **Anchors sliced through proper nouns.** On the Saudi corpus the first run produced
   `Saudi Data and Artificial Intelligence` (from "...Authority (SDAIA)") and
   `Data and Artificial Intelligence` (from "National Strategy for..."), linking an
   organisation and a policy document to a market report. Fixed by `_names_a_longer_entity`;
   plans fell from 7 to 5 and the two bad ones are gone.
2. **Noisy editorial titles were sliced.** "How a Global Food Leader Found Many Opportunities
   in KSA Catering" yielded 63 prose fragments. A phrase longer than
   `placement.max_anchor_words` is now never sliced: too long to be an anchor itself means it
   is not a market name.

A third, caught by an existing test: sorting the candidates by length broke ADR-0027's rule
that the geography LABEL precedes its aliases. The sort is stable on rank alone again.

## Alternatives rejected

- **Rewrite the sentence to contain the keyword** (ADR-0023). Still the right answer for pairs
  with no context at all, but it is LLM copy into production prose and a much larger decision.
  This ADR marks up text that already exists.
- **Score the anchor instead of gating it.** ADR-0027 moved deliberately from scores to gates
  because a score lets a bad pair through on the strength of unrelated signals.
- **Let the model propose anchors.** Deterministic sub-phrases of the target's own title are
  explainable and reproducible; model-chosen anchors are neither, and CLAUDE.md prefers
  deterministic code wherever it suffices.
