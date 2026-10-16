# Suite: anchor-selection

> Phase 0: structure only, with no cases. First populated in **Phase 3**
> (contextual placement) and **Phase 4** (Planner node). It becomes a formal
> dataset in Phase 8. TRD §19.

**Exercises:** `PLACE` (Planner agent producing `LinkPlan` objects, TRD §10).

**A passing case:** the anchor is text that **already exists** in the source
paragraph, it reads naturally for the destination, and the plan carries a
paragraph identity plus deterministic offsets or another stable selector that
resolves to exactly that text (PRD §7.7).

**Example cases (prose):**
- A paragraph that names a market should anchor on that exact phrase, not on a
  generic phrase like "click here" or "this report".
- When the only good phrase falls inside an existing link, the expected
  outcome is no placement in that paragraph.
- When the model returns an anchor string that is absent from the source, the
  case is a hard fail. Fabricated text is never acceptable.

**Failure modes guarded:** fabricated or rewritten source text; offsets that
do not match the anchor; anchors nested in existing links or headings;
over-long or keyword-stuffed anchors; placement outside body content
(navigation and footer are non-goals, PRD §6).

## Phase 8 status: populated (14 cases)

Dataset: [`evals/datasets/anchor-selection/core.v1.jsonl`](../../datasets/anchor-selection/core.v1.jsonl).
Gate: `min_pass_rate` 0.80, `max_regression_vs_baseline` 0.05.

Drives `placement.anchor_phrases` and `placement.find_placement` over blocks parsed from the
fixture's HTML by the **production normaliser**, so text runs and enclosing elements are real.
A hand-built `ContentBlock` has no runs and `span_is_safe` refuses every span in such a block,
so hand-building blocks would have made every case vacuously "no placement".

The decision is `anchor:<text>@<block_id>` or `no_placement`. Every case carries
`must_not: ["fabricated_anchor"]`, which the runner raises whenever the chosen anchor is not
byte-identical to `block.text[start:end]` — so fabrication is a zero-tolerance failure in all
14 cases, not one of them.

Covered: topic-alias anchors; no-phrase pages; a phrase only inside an existing link; headings;
table cells; blockquotes; generic phrases present alongside the real one; geography labels
(`India` must never link to a market page); a block at the per-block link cap falling through to
a list item; blocks already linking the target; an already-taken span; the opening-block
quality penalty; and both halves of the PRD §12.4 own-market rule — the phrase is an anchor
only where the block also names the destination's distinctive geography, and yields no
placement where it does not.
