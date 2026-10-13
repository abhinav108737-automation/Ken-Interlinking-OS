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
