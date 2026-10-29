# ADR-0023 — A link plus the wording around it: contextual copy proposals

- **Status:** Accepted by the owner for planning, 2026-10-29 (answers recorded at the end).
  **Nothing here is implemented.**
- **Decision owner:** the project owner.
- **Supersedes:** ADR-0020 decision 2 ("insert the single word *market* only") and the resolution
  of decision 4 ("report data is used only to judge relevance"). ADR-0020 decision 3
  (`operator_admin` only approves) is kept, as the owner confirmed.
- **Authority:** owner direction of 2026-10-29: add the hyperlink **and** rewrite or adjust the
  page wording so the link carries context and a real insight, driving intent-based clicks rather
  than compliance. CLAUDE.md: no LLM writes to production; every LLM output crossing a service
  boundary is validated; every mutation is idempotent, version-aware, auditable and reversible;
  architecture changes need an ADR.

## Context

Plans today do one thing: turn an existing phrase into a link, leaving every other byte of the
page identical. That is why the owner's four rejections on 2026-10-28 (anchors judged too
generic) could not be satisfied by the engine.

The owner now wants two operations together:

1. **The hyperlink** — what the system does today.
2. **Contextual wording** — the sentence around the link changed, or a short sentence added, so
   the reader gets a reason to click: an insight the target page actually supports.

I read the goal as **"earn the click with a genuine, relevant insight"**, not "compel it with
pressure". Everything below is built to that reading. If the owner means something closer to
urgency or curiosity-bait, this ADR does not cover it and I would recommend against it: it
misleads readers, damages trust in a research publisher, and is exactly the kind of text a
reviewer should reject.

## Purpose, as the owner stated it

Internal linking for SEO, driven by measurement. Read analytics to learn **which URLs earn
traffic and which do not**, then direct internal links toward the pages that need it, so that
traffic reaches them and they are discovered and indexed.

This is why the edit must be data-led: a sentence that tells a reader a market has real potential
gives a reason to click, and gives the destination page genuine relevance, which is what an
internal link is meant to signal. It is also why ADR-0022 (analytics authentication) is on the
critical path: without click and impression data there is nothing to steer by.

One honest limit: how much a given internal link moves a page in search is **not something this
repository can show**. The system will report associations with their confounders and never claim
that a link caused a ranking change (the Phase 7 rule).

## What this changes, plainly

Today the system can only **mark up** words that are already there. This ADR lets it **propose
new words**. That is a different category of risk: the output is published copy under Ken
Research's name, and a fluent sentence can be wrong. The design therefore treats every proposed
sentence as a **claim that must be traceable**, not as prose.

## Decision

### 1. One plan, two parts, both reviewed together

A plan gains an optional **copy edit** beside its link. The reviewer sees one before/after of the
block: the original text, the edited text, the link, and the evidence. Approving approves both.
A plan with no copy edit behaves exactly as today. Nothing existing changes.

### 2. What an edit may be

- **Scope:** the sentence containing the anchor, rewritten; or one sentence added directly
  before or after it. One block, one edit per plan.
- **Size:** bounded by policy: at most 35 words added or changed (owner-approved).
- **A longer edit must carry data (owner decision).** An edit over 20 words must contain at least
  one quantitative fact (a market size, growth rate, share or forecast) that appears in an
  evidence span from the target page. The sentence earns its length by telling the reader why the
  destination matters. The 20-word threshold is a starting value for the owner to adjust.
- **No new facts of its own.** Every number, date, named entity and comparative in the new text
  must appear in an **evidence span**: a verbatim quotation from the target page (or the source
  page), stored with page id, version, block and offsets.

### 3. Deterministic checks decide; a model only drafts

A model (local, per the project rules) may **propose wording**. It never decides acceptance.
Acceptance is deterministic and fails closed:

| Check | Rule |
|---|---|
| Provenance | every number, date and entity in the new text appears in a stored evidence span |
| Preservation | text outside the edit is byte-identical; the original is stored for exact rollback |
| Placement | not inside an existing link, heading, table, hidden or protected block |
| Tone lint | refuses urgency and bait: "you won't believe", "shocking", "act now", "don't miss", "secret", "must-read", ALL-CAPS runs, stacked exclamation marks, a question with no answer in the target |
| Relevance | the target page's own analysis supports the stated insight (same market or entity), measured by the existing score, not by the model's say-so |
| Length | within the policy word limit |

The model's output is validated with Pydantic at the boundary (constraint 9). A response that
fails validation is a fallback to a plain link, never a repaired guess.

### 4. Intent, from data that already exists

Page intent (`commercial_research`, `informational`, …) is already computed per page. It selects
the **style** of the insight (a market-size fact for a commercial reader, a definition for an
informational one) from a small versioned set of patterns in a policy file. It does not let a
model invent a persuasion strategy.

### 5. Approval now, autonomy as the destination

**Now:** only an `operator_admin` may approve a plan containing a copy edit (owner decision).
The autonomy engine does not decide these plans, in any mode.

**No editorial-team sign-off is required** (owner decision): the wording is generated by agents
and a model, and the owner approves it. Nothing in this ADR adds a human step beyond the approval
and the CMS's own publish.

**Destination:** the owner wants copy edits to run autonomously, end to end, by agents. That is
the same trajectory the whole project follows ("progressively earn autonomy for proven decision
classes"), so it is in scope, but it is **reached, not switched on**. Under the learning rules
(CLAUDE.md) autonomy is never self-granted: evidence, then offline evaluation, then shadow, then
an explicit human promotion. For copy edits that means, as proposed starting criteria the owner
may change:

| Gate | Proposed requirement |
|---|---|
| Sample | at least 30 reviewed copy edits (the existing sample floor), not 5 |
| Accuracy | zero reviews filed as `edit_inaccurate` in the window |
| Provenance | zero shadow edits that failed the provenance check after passing the others |
| Outcome | the holdout shows no harm to clicks on the pages that received edits |
| Promotion | an explicit human promotion of a new policy version, recorded and reversible |

Until every gate is met the answer is manual approval. When they are, switching to autonomy is a
**new policy version the owner promotes**, with an ADR recording it, and not a code change.

**Publishing.** Approval writes through the CMS API to a **draft**. The owner confirmed on
2026-10-29 that the CMS has a separate publish step. Autonomy for *drafting* and autonomy for
*publishing* are separate questions: this ADR does not assume the publish step is removed, and
whether an agent may trigger it is a decision for the backend team and the owner later.
- Rollback restores the stored original block text exactly. If the page changed since, it
  refuses (409) and overwrites nothing.

### 6. Measure whether it works, without claiming causation

The owner's hypothesis — that contextual wording earns more intent-based clicks than a plain
link — is **not established by anything in this repository**. Treat it as a hypothesis and test
it:

- Use the existing **holdout** experiment mechanism (`contracts/.../learning.py`, a hard cap on
  the share of pages that may be held out) to give some pages a plain link and others link plus
  context.
- Read clicks and impressions from the analytics importers (ADR-0022 must land first).
- Report **association with confounders listed, never causation** (the Phase 7 rule).

### 7. Learning

Reviews must carry structured reasons or the learning step cannot read them. Proposed additions
to `FeedbackReason`: `edit_inaccurate`, `edit_too_promotional`, `edit_off_topic`,
`edit_good_as_is`. Memory updates remain proposals a human promotes (ADR-0021).

## Risks

- **A fluent sentence can be false.** Mitigated by provenance checks, but a claim can be
  *traceable and still misleading by omission*. A human reviewer is the last defence, which is
  why this is never autonomous.
- **Small local model.** The configured model is `qwen3:4b`. Drafting quality will be uneven and
  the fallback rate high. A larger local model (for example an 8B) would help and is allowed, as
  it is free and self-hosted, but it costs download time and memory. Expect many plans to fall
  back to a plain link; that is correct behaviour, not a defect.
- **Editorial and legal.** Changing published research copy may need Ken Research's editorial
  sign-off and a record of who approved each wording. Not decided here.
- **Search-quality risk.** Mass-inserted, keyword-led sentences across many pages can read as
  low-quality or manipulative to readers and to search engines. Start small and measure.
- **Evidence quality.** One reviewer, five reviews, demo data. A pattern learned from that is one
  person's taste, not a result.

## Consequences

New plan fields (original text, edited text, evidence spans, edit kind), a migration, a drafting
skill with a Pydantic output contract, the deterministic checks above, a tone-lint policy file, a
generalised splice from "insert one `<a>`" to "replace one block span", a before/after screen,
approval gating by role, new feedback reasons, evaluation fixtures including negative cases
(a changed number, an unsupported claim, bait phrasing, a duplicate insertion, a stale source),
and a shadow pass before anything reaches a draft. This is a full phase; it is **not** today's work.

## Proposed order

1. **Plain hyperlink through the Django API.** Already specified for the backend team. This is
   the foundation, it delivers value on its own, and everything below depends on it.
2. **Copy proposals in shadow only.** Generated and shown in the review screen, written nowhere.
   The owner reads them and judges quality before any publish path exists.
3. **Drafts via the CMS** for approved plans, with a human publish.
4. **A holdout test** of link-only versus link-plus-context, read from analytics.

## Owner answers (2026-10-29)

1. **The reading is right**: an insight that earns the click. The purpose is SEO internal linking
   steered by analytics (see "Purpose").
2. **35 words and one edit per plan are accepted**, with the condition that a longer line must
   hold data that shows the destination market's potential (see section 2).
3. **No editorial-team sign-off.** The owner wants wording generated autonomously by agents and a
   model, with a small editorial process only as a fallback if needed. Recorded in section 5 as
   manual approval now and earned autonomy later.
4. **A larger free local model is permitted** for drafting. `qwen3:8b-q4_K_M` (4.87 GB) was
   downloaded and loaded on 2026-10-29. It is self-hosted and free, so the no-paid-API rule is
   unaffected. A first, single-sample drafting test (one 30-word context sentence from two given
   facts) found `qwen3:4b` emitting its reasoning text instead of the sentence, 95 words against
   a 30-word limit, so it would fail validation and fall back to a plain link. `qwen3:8b` produced
   one 13-word sentence that used only the supplied facts. **One sample is an anecdote, not an
   evaluation**: the real comparison is the evaluation fixtures in section 3's consequences,
   which do not exist yet. No role in `.env` was switched.
5. **`operator_admin` stays the only approver** for plans with a copy edit, for now.

## Still open

- Whether an agent may eventually trigger the CMS's publish step, or a person always does. That
  is a backend-team and owner decision for later; it does not block planning or the draft stage.
- The 20-word data threshold and the autonomy gates above are starting values, not final.
