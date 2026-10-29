# ADR-0020 — Keyword-bearing anchors: may the system propose a minimal sentence edit?

- **Status:** Accepted by the owner for planning, 2026-10-28 (answers recorded below). **Nothing
  in this ADR is implemented.** The section 5 gates (eval fixtures, shadow run, v2 policy, human
  promotion) still apply before anything is switched on.
- **Decision owner:** the project owner (the reviewer account `abhinav.kumar`).
- **Would amend if accepted:** ADR-0011 section 3 (anchors are copied verbatim), ADR-0012
  `check_invariants` (anchor verbatim), ADR-0013 ("verbatim, never fabricated"), ADR-0014
  (a deployment inserts one `<a>` and every other byte is identical), and ADR-0018 (autonomy).
- **Authority:** CLAUDE.md constraint 17 (architecture decisions are not changed silently) and the
  learning rule "experience -> structured feedback -> proposal -> offline eval -> shadow/canary ->
  explicit promotion".

## Context

On 2026-10-28 the owner reviewed five link plans (`review_actions` ids 1-5):

| # | action | anchor | target | stated reason |
|---|---|---|---|---|
| 1 | approve (`good_as_is`) | "charging infrastructure" | India EV charging infrastructure market | none |
| 2 | reject (`other`) | "Auto parts" | India auto components market | add "market" to the end; a stronger anchor emphasises the keyword for search |
| 3 | reject (`other`) | "rooftop solar" | Vietnam solar power market | same; adding words is acceptable if highly contextual and intent-mapped |
| 4 | reject (`other`) | "electric vehicles" | Thailand EV market | same |
| 5 | reject (`other`) | "wafer fabrication" | India semiconductor market | same |

The requested anchors ("auto parts market", "rooftop solar market", ...) do **not** occur in the
source text: in all four rejected plans the word following the anchor is not "market". The engine
links only text that is already on the page (ADR-0011 section 3), so it cannot produce them.

Today 46 of 48 anchors are 1-2 words, although `max_anchor_words` is 8.

Evidence limits: five reviews, one reviewer, no click, ranking or outcome data, and the demo corpus
(`research.example.com`), not production content. The learning pipeline needs 30 samples per
decision class (`min_sample_count: 30`) and its reflection step found 0 usable samples.

## Question

Should the system be allowed to **propose** a plan that changes the page's own wording (for
example "auto parts" -> "auto parts market") so that the anchor can carry the keyword?

## Options

- **A. Prefer longer anchors that already exist in the text.** Stays inside the verbatim rule.
  Cannot add "market" where the page does not say it.
- **B. Allow a minimal copy edit as a new kind of plan.** This ADR.
- **C. Change nothing; collect structured feedback until the sample floor is met.**

A and C are compatible with each other. B is not required by either.

## Proposed decision (if the owner accepts B)

### 1. A copy edit is a different plan kind, never a link plan with a looser rule

A new `plan_kind = anchor_copy_edit`. The existing `link` kind keeps its verbatim invariant
unchanged. Every check that exists today for `link` plans still runs on that kind.

### 2. The edit is constrained and deterministic

- **Insert only the single word "market"** (owner decision 2), immediately after the anchor
  phrase, and only where the target is a market page. No other words, so no allowlist is needed.
- **No model writes page text.** A model may only judge a proposal; it never supplies the words.
- At most one insertion of one word per plan, inside one block, not inside an
  existing link, table, heading or protected block (the placement exclusions already in the policy).
- The edited sentence must not change a number, a named entity, a date or a claim. The check is
  deterministic: the token multiset outside the insertion must be unchanged.
- Both the original and the edited block text are stored on the plan and shown as a before/after
  diff.

### 3. Human approval is mandatory for this kind, always

Copy edits are excluded from the autonomy engine (ADR-0018). They are never auto-published, in any
mode, whatever the confidence. **Only an `operator_admin` may approve one** (owner decision 3);
reviewers and viewers cannot. `AUTO_APPROVE` does not apply.

### 4. Deployment, verification and rollback

- The splice (ADR-0014) is generalised from "insert one `<a>`" to "replace one block span", still
  version-locked and idempotent, and still leaving every other byte identical.
- The post-publish verification checks the edited text and the link.
- **Rollback restores the original block text exactly**, not only removes the link. The original is
  kept with the plan.
- Every step is audited with typed events, as for links.

### 5. Evidence required before this can be switched on

1. Evaluation fixtures for this plan kind, including negative cases: changed numbers, entities,
   claims, a duplicate insertion, a protected block, a stale source version.
2. A shadow run on the existing corpus recording what **would** be proposed, with no writes.
3. A new `recommendation.v2` policy (v1 is immutable). A human promotes it.
4. Explicit owner sign-off on editorial and SEO risk (below).

## Risks

- **Editorial and legal:** the system would be changing published copy, not only adding links.
  Brand, accuracy and sign-off processes at Ken Research may require a human editor, not only this
  reviewer.
- **Over-optimisation:** repeating the target's keyword in anchors across many pages can look like
  keyword stuffing and may hurt, not help. That the keyword-rich anchors improve search results is
  the owner's hypothesis. It is **not** established by anything in this repository. The analytics
  phase can measure it, and it must not claim causation (Phase 7 rule).
- **Blast radius:** a bug here alters live content. This is why section 3 forbids autonomy and
  section 4 requires exact rollback.
- **Four examples:** the sample is too small to justify a permanent architecture change on its own.

## Consequences if accepted

New plan kind, contract and migration, new invariants, a generalised splice, new eval fixtures,
a v2 policy, UI diff support in the review screens, and tests for every new endpoint. A realistic
estimate is one full phase of work.

## Owner answers (2026-10-28)

1. **Editing published copy is in scope.** Yes.
2. **Insertable word: "market" only.** The allowlist in section 2 narrows to that single word
   (an anchor such as "auto parts" becomes "auto parts market"). Maximum insertion: one word.
3. **Approval: `operator_admin` only.** A reviewer or viewer cannot approve this plan kind.
4. **Anchor length and context.** The owner's words: "Anchor should be targeting only market
   keywords, no longer anchors. But for context you can add data from report."
   - Understood: anchors stay short (a market keyword plus "market"), so **option A (longer
     verbatim anchors) is not wanted**.
   - **Resolved (2026-10-28):** "add data from report" means the target report's data is used
     **only to judge whether the link is contextual and to explain it**. No report text is written
     into the source page. The only page change this ADR permits is inserting "market". Copying
     report sentences or statistics into published copy would add claims and is **out of scope**; it
     would need its own ADR.

Sections 2 and 3 above already reflect answers 2 and 3.

## Status log

- 2026-10-28: drafted and left **Proposed**. No code, policy or data changed.
