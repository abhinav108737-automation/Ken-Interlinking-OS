# Business Phase 2 — repository-backed gap analysis

- **Date:** 2026-11-19
- **Product intent:** [`knowledge/BUSINESS_PHASE_2_PRIORITY_INTERLINKING.md`](../../knowledge/BUSINESS_PHASE_2_PRIORITY_INTERLINKING.md)
- **Handover:** [`docs/handover/BUSINESS_PHASE_2_CLAUDE_HANDOVER.md`](BUSINESS_PHASE_2_CLAUDE_HANDOVER.md)
- **Status:** analysis ACCEPTED by the owner 2026-11-19. The §9 decisions were taken (§10).
  **Slice 2A-1 is implemented and verified (§11).** Steps 3–7 of the owner's sequence are not
  started, by instruction.
- **Method:** every claim below is read from the repository at this commit. File and line
  references are real. Where I did not trace behaviour, it is marked **(verify)**.
- **Baseline measured:** `tests/unit` — 4,657 passed, 32 skipped (platform-specific) on
  2026-11-19. The four known-red integration tests of NEXT_STEPS item 34 were not re-run here
  (they need a database); item 34 records their cause.

---

## 1. Executive summary

The engine already implements more of Business Phase 2 than the knowledge base assumes. The
genuine gaps are concentrated in four places, and three of them are **policy data and owner
decisions, not missing code**:

| Finding | Nature |
|---|---|
| Page-family routing covers 3 of 12 families | **Policy data.** Mechanism exists. NEXT_STEPS item 29 explicitly reserves the decision to the owner. |
| No per-page priority registry | **Genuine gap.** Priority exists only as a *page-type* property, not a per-page designation. |
| Topic hubs cannot be link sources | **Taxonomy data.** `hub` carries `linkable_as_source: false`; the corpus holds exactly **1** hub page. |
| Module items cannot be measured | **Genuine structural gap.** `link_events.plan_id` is a foreign key to `link_plans`; a module item is not a plan. |

Two further structural facts shape everything:

1. **The eval corpus is inert under ADR-0027** (item 34). Four suites already fail with
   *"engine proposes [], fixture records [...]"*. Phase 2A deliverable #4 asks for fixtures
   covering priority, hub, cross-product, geography, stale, protected and rejected cases. Those
   fixtures cannot be *recorded* against a corpus on which the engine proposes nothing.
2. **The market graph is empty** (items 27 and 32). 0 of 5,451 markets carry an `adjacent` /
   `complementary` / `upstream` / `downstream` edge. Business Phase 2 §5.5 requires governed
   typed market relationships. The gate clause that would use them is unreachable code today.

Nothing in Business Phase 2 requires a second recommendation engine, and nothing in it conflicts
with ADR-0027's gates. Its §4 ("rules that must not be implemented literally") is *already* the
repository's behaviour in three of five cases — see §3.

---

## 2. Requirement-by-requirement mapping

### 2.1 Priority-page registry (knowledge §5.1)

**Exists.**

| Capability | Where |
|---|---|
| `commercial_importance` 0–3 per **page type** | `contracts/python/ken_contracts/intelligence.py:191` (`PageTypeRule`), `:325` (`PageAnalysis`) |
| `commercial` ranking component, weight 0.05 | `policies/ranking/ranking_weights.v1.yaml`; closed name set at `contracts/python/ken_contracts/recommendations.py:397` |
| `destination_priority` score input | `services/ken_services/recommendations/scoring.py:270`, `generators.py:632` |
| `HIGH_COMMERCIAL_VALUE` reason code | `contracts/python/ken_contracts/recommendations.py:133` |
| Scoring runs **after** gates | `gates.py` module docstring: gates run "BEFORE scoring" |

**Reuse / extend.** The post-gate ordering is already correct by construction: `market_gate` and
`geography_gate` refuse a pair outright, and scoring never sees it. Priority therefore *cannot*
bypass a gate if it is expressed as a scoring input. This satisfies knowledge §4's "priority is
never a licence to override relevance" without new enforcement code.

**Genuine gap.** There is no **per-page** designation. Today a page's commercial weight is
inherited wholesale from its page type — every `report` scores 3, every `survey` 2. Business
Phase 2 requires per-page records with: priority type (9 named kinds), weight/tier, reason,
owner, effective and expiry dates, active state, and created/updated audit. None of that exists
in any table. `migrations/versions/` ends at `0012_page_modules.py`.

**Conflict.** Adding a distinct `priority` ranking component is a **frozen-contract change**:
`RANKING_COMPONENTS` is a closed tuple of 8 names and `ScoreComponent` validates membership
(`recommendations.py:355`). `docs/CONTRACTS.md` line 3 freezes the module and line 6 states a
specialist "proposes the change to the lead orchestrator. It does not edit the file."

**Two implementation routes:**

| Route | Contract change? | Explainability | Verdict |
|---|---|---|---|
| **(a)** Feed governed priority into the existing `commercial` component | **No** | Limited — `HIGH_COMMERCIAL_VALUE` cannot say *which* priority type fired | Viable for 2A |
| **(b)** Add a `priority` component + per-type reason codes | **Yes** — frozen contract + `ranking_weights.v2.yaml` | Full | Needs ADR + owner approval |

Knowledge §5.5 proposes priority at 10% against the current `commercial` 0.05, which is a
`ranking_weights.v2.yaml` either way. Knowledge §5.5 itself calls these "starting hypotheses,
not permanent magic numbers."

**Work split.** DB migration (new table + audit columns); service read path; policy version;
*no* UI in 2A. Contract work only on route (b).

---

### 2.2 Topic hubs (knowledge §5.2)

**Exists.** `hub` is a declared page type in `policies/taxonomy/taxonomy.v3.yaml`
(intent `navigational`, `commercial_importance: 1`), and it is already a permitted *target* in
`recommendation.v1.yaml` (`report: [report, insight, hub]`).

**Blocking facts, both measured:**

1. `hub` carries **`linkable_as_source: false`** in taxonomy v3. A hub cannot propose links out.
   Knowledge §5.2 requires a hub to present reports, articles, surveys, consulting and expert
   routes — i.e. to be a source. Changing this means **taxonomy v4**; a taxonomy is never edited
   in place (item 26: "a taxonomy is never edited in place").
2. The `tax3-alg2` corpus holds **exactly 1 hub page** against report 7,085 (item 29). There is
   no hub inventory to compute membership for.

**Genuine gap.** No hub-membership model, no membership reason codes, no curation path.
`ModuleKind` (`recommendations.py:810`) has exactly one member, `REGIONAL_SIBLING`, and its
docstring states additions are deliberate and governed.

**Conflict with knowledge §5.2 itself.** The document requires hubs to be *curated* — "a hub is
a curated decision page, not an automatically generated doorway page" — and §9 lists
"auto-generating thin SEO doorway hubs" as an explicit non-goal. Curation input does not exist
in the repository. Generating hub membership from the corpus without curation would violate the
knowledge document that authorises the work.

---

### 2.3 Cross-product journey graph (knowledge §5.3)

**Exists.** Two independent layers already gate page-family routing:

| Layer | Where | Current coverage |
|---|---|---|
| Taxonomy eligibility | `linkable_as_source` / `linkable_as_target` on `PageTypeRule` | all 12 families |
| Routing policy | `page_type_targets` in `recommendation.v1.yaml` | **3 source families, 3 target families** |

Measured from `policies/taxonomy/taxonomy.v3.yaml`:

| Page type | intent | comm. | source? | target? |
|---|---|---:|---|---|
| report | commercial_research | 3 | yes | yes |
| benchmark | commercial_research | 3 | yes | yes |
| survey | commercial_research | 2 | yes | yes |
| consulting | commercial_research | 2 | yes | yes |
| case_study | informational | 2 | yes | yes |
| pov | informational | 2 | yes | yes |
| insight | informational | 2 | yes | yes |
| brand_comparison | commercial_research | 1 | yes | yes |
| other | informational | 1 | yes | yes |
| hub | navigational | 1 | **no** | yes |
| transactional | transactional | 1 | **no** | **no** |
| corporate | corporate | 0 | **no** | **no** |

`recommendation.v1.yaml` declares only:

```yaml
page_type_targets:
  report: [report, insight, hub]
  insight: [report, insight, hub]
  other: [report, insight]
```

So `survey` (906 pages), `benchmark` (496), `case_study` (55), `pov` (53), `consulting` (7) and
`brand_comparison` (9) are **eligible in the taxonomy but unroutable in the policy** — item 29's
166,576 `page_type_incompatible` refusals.

**Genuine gap — a page family that does not exist.** Knowledge §5.3 requires "expert routes"
and "expert interview → related report or service". There is **no `expert` page type** among
the 12. Supporting it requires taxonomy v4, not a routing-policy edit.

**Conflict / decision.** Item 29 states verbatim: *"**Needs your decision, not ours:** which
families may be sources, which may be targets."* This is Phase 2A deliverable #2 and it is
reserved to the owner. The mechanism needs no code — it is `recommendation.v2.yaml`.

---

### 2.4 Next-best-action recommendations (knowledge §5.4)

**Exists.** `Intent` (5 values, `intelligence.py:77`), the `intent_compatibility` matrix in
`recommendation.v1.yaml`, structured `ReasonCode`s, and deterministic rationale rendering
(`services/ken_services/recommendations/rationale.py:67`). Every plan already records
`policy_version`, `ranking_policy_version`, `analysis_version` and `engine_version`
(`recommendations.py:661-674`).

**Genuine gap.** There is no *action* concept. Knowledge §5.4 requires each recommendation to
record an **intent class**, a **placement surface**, and a destination-or-action — where the
action may be a CTA ("Download the complete market sample") rather than a page. `LinkPlan` has
no surface field and no action field; its destination is always a page.

**Work split.** Contract (new fields or a new model) + service + UI. Not 2A.

---

### 2.5 Recommendation ranking (knowledge §5.5)

**Exists.** `ranking_weights.v1.yaml` weights exactly the 8 closed component names;
`ScoreBreakdown` (`recommendations.py:362`) enforces `relevance = sum(contributions)` and
`total = relevance × prod(factors)`, both checkable from the stored record alone. Component
values are recorded per plan — the explainability knowledge §5.4 asks for already exists for
scoring.

**Mapping the proposed weights to what exists:**

| Knowledge §5.5 signal | Proposed | Existing component | Current |
|---|---:|---|---:|
| Same topic / governed market relationship | 30% | `taxonomy` | 0.20 |
| Same industry or sub-industry | 20% | (part of `taxonomy`) | — |
| Compatible country or region | 20% | `geography` | 0.08 |
| Matching reader intent | 15% | `intent` | 0.15 |
| Governed business priority | 10% | `commercial` | 0.05 |
| Freshness | 5% | *none* | — |

**Genuine gaps.** (i) There is no `freshness` ranking component — `freshness.py` exists but
serves staleness checks, not ranking. Adding one is a frozen-contract change
(`RANKING_COMPONENTS`). (ii) `semantic`, `graph`, `underlinked` and `historical` carry 0.52 of
current weight and appear nowhere in the knowledge table; adopting the table literally would
delete them.

**Conflict — the load-bearing one.** Knowledge §5.5 requires market adjacency to use governed
typed relationships (`component_of`, `complement_of`, `upstream`, `downstream`,
`substitute_of`). Item 27 measures **0 of 5,451 markets** carrying any such edge, so
`MARKET_GATE_TYPES` (`gates.py:83`) can only ever match `SAME_MARKET`, `PARENT_MARKET` and
`SUBMARKET` today. Item 32 is the design for closing this and is marked **ASK before adding an
agent node**.

---

### 2.6 Placement surfaces (knowledge §5.6)

**Exists — and the exclusions knowledge §5.6 demands are already enforced.**

| Surface | Status |
|---|---|
| 1. Contextual body link | **Built.** `placement.py`, `block_is_prose:591` |
| 3. Related reports by country/region | **Built.** `modules.py`, `ModuleKind.REGIONAL_SIBLING`, persisted in migration 0012, served at `GET /recommendations/modules`, reviewer screen at `/recommendations/modules` |
| 2, 4–9 (hub membership, adjacent markets, related articles, surveys, consulting, expert, "next step") | **Absent.** |

Placement exclusions are enforced in two layers: `placement.block_kinds: [p, li]` and a
29-element `excluded_ancestors` list in `recommendation.v1.yaml`, plus `block_is_prose`.
Knowledge §5.6's prohibition list (TOC, navigation labels, headings, tables, hidden content,
methodology boilerplate) is a **subset** of what is already refused. No new enforcement needed.

**Genuine gap.** `LinkPlan` carries no `surface` discriminator, so "inline vs module" cannot be
expressed on a plan, chosen by a reviewer (knowledge §5.11), or measured (knowledge §6).

---

### 2.7 Two-way connection checks (knowledge §5.7)

**Absent entirely.** No reciprocity analysis exists. `GraphSignals` (`recommendations.py:676`)
reports per-page link-graph position, which is the input such a check would need, but no code
evaluates whether a useful return edge exists. Knowledge §5.7 correctly requires that a return
edge pass the same gates rather than be forced — so this is a new generator, not a bypass.

---

### 2.8 Link and anchor quality (knowledge §5.8)

**Exists, and already matches the knowledge document's intent.**

- `forbidden_anchors: [click here, here, read more, learn more, ...]` in `recommendation.v1.yaml`
- `min_anchor_words` / `max_anchor_words` (1–8), `min_anchor_chars`
- `ANCHOR_IS_TARGET_MARKET_KEYWORD`, `ANCHOR_NAMES_TARGET_GEOGRAPHY` reason codes
- `anchor_too_generic` structured feedback reason (`review.py`)
- ADR-0027 §3/§4: the anchor must be existing page text and must name the target's geography
  when it differs

**Alignment, worth stating:** knowledge §4 says *"Do not enforce an 80/20 anchor formula"* and
§5.8 says *"There is no fixed anchor percentage requirement."* The repository has **no
percentage anywhere** in anchor selection. The knowledge document and the code already agree.

**Genuine gap.** No anchor **history per target** and no repetition warning. Knowledge §5.8 asks
for warnings on excessive repetition and on multiple links crowded into one paragraph; the
latter is partly covered by `max_links_per_block: 1` and
`max_internal_links_per_100_words: 2.0`, the former not at all.

---

### 2.9 Freshness and lifecycle (knowledge §5.9)

**Exists.** `freshness.py`, `FreshnessCheck` contract, `LinkPlanStatus.EXPIRED` ("the source
changed so the plan is STALE"), and `context_changed` / `anchor_moved` diagnostics (NEXT_STEPS,
Phase 2 limitations). Version checks, idempotency, verification and rollback are Phase 6
machinery and are intact.

**Genuine gap.** No **fan-out**: when a report is published or superseded, nothing identifies
the affected older articles, hubs, regional siblings, service pages or high-traffic entry pages.
Knowledge §5.9 requires that list. Note it also requires *recommending* updates, never silently
rewriting — which matches the existing "no LLM writes to production" constraint.

---

### 2.10 Quality and opportunity audits (knowledge §5.10)

**Exists.** `OpportunityKind` (`recommendations.py:790`) with three members — `ORPHAN`,
`UNDERLINKED`, `LINK_CAPACITY` — persisted per run and surfaced at
`/recommendations/opportunities`.

**Genuine gaps.** Knowledge §5.10 names 11 audit classes. Missing: hubs missing expected asset
types; broken or redirected destinations; links to empty filters / temporary URLs /
non-canonical variants; stale links to superseded reports; priority pages too deep; contradictory
trust claims. Also missing: the four **severity levels** (critical / high opportunity / medium /
low) — `OpportunityView` carries a `score` float, not a severity.

---

### 2.11 Editorial controls (knowledge §5.11)

**Exists.** `ReviewAction` = approve / reject / edit_anchor / replace_target, each with a
constrained `FeedbackReason` set (`review.py:69-127`), and a reviewer UI at
`/recommendations`, `/recommendations/[planId]`, `/recommendations/candidates`,
`/recommendations/modules`, `/recommendations/opportunities`.

**Genuine gaps.** Four of the eight controls knowledge §5.11 lists have no equivalent:

| Control | Status |
|---|---|
| approve / reject | exists |
| replace an outdated target | exists (`replace_target`) |
| see why a recommendation was made | exists (`rationale`, reason codes) |
| **choose inline vs module placement** | absent — requires the surface field (§2.6) |
| **move a recommendation to a section** | absent |
| **lock a good existing link** | absent |
| **suppress a source–target pair** | partial — `rejection_cooldown_days: 90` expires; no permanent suppression |
| **see the expected journey / business destination** | absent |

---

### 2.12 Measurement (knowledge §6)

**Exists.** `link_events` (first-party, no PII), `link_metrics_daily`, `page_metrics_daily`,
`LinkMeasurement` written in the same transaction as `verified -> measured`, GA4 + Search Console
import proven live (item 15), and `ConfounderFlag.SYNTHETIC_DATA` refusing to measure synthetic
data (`analytics.py:498`).

**Genuine structural gap — the most important one in this document.**
`link_events.plan_id` is a **foreign key to `link_plans`** (`migrations/versions/0008_analytics.py:186`)
and `LinkMetrics.link_id` is `str(plan_id)` (`analytics.py:24`). A **module item is not a plan** —
it lives in `page_module_items` (migration 0012) with no plan id and, by design, no status.

Therefore: **the 3,213 persisted modules and 9,777 module items cannot be measured at all.**
Knowledge §6 requires attribution by "link or module item identity" and by "placement surface
and position". Neither is expressible today. Since modules are ~10× the linking volume of inline
plans (item 31), this gap covers most of the intended Business Phase 2 surface area.

Knowledge §6's remaining dimensions — intent class, priority classification, topic cluster,
experiment version — have no analytics columns either.

**Correctly aligned:** knowledge §6 ends *"Do not claim causation from ordinary before/after
analytics"*, which matches `ConfounderFlag` and the existing holdout governance (item 21).

---

## 3. Where the knowledge base and the repository already agree

Knowledge §4 lists five source-document rules that "must not be implemented literally". Three are
already the repository's behaviour, and recording that prevents re-litigating them:

| Knowledge §4 rule | Repository state |
|---|---|
| Body links are not the only useful links | Modules exist and are the primary Priority-1 surface (item 31) |
| Do not enforce an 80/20 anchor formula | No percentage exists anywhere in anchor selection |
| Do not promote a page only because it is marked important | Gates run before scoring; priority can only ever reorder survivors |
| Do not ban every cross-silo link | **Partially open** — `MARKET_GATE_TYPES` allows complementary/supply-chain relations, but 0 such edges are declared (item 27), so cross-topic links are refused in practice |
| Do not force every leaf page to link to a parent in a paragraph | No such rule exists |

---

## 4. Conflicts requiring an ADR or an owner decision

| # | Conflict | Kind | Blocking |
|---|---|---|---|
| C1 | Which of the 12 page families may be sources and which targets | **Owner decision** (item 29 verbatim) | Phase 2A #2 |
| C2 | `hub.linkable_as_source: false`; only 1 hub page in the corpus | **Taxonomy v4 + curation data** | Phase 2A #3 |
| C3 | No `expert` page family exists | **Taxonomy v4** | knowledge §5.3 |
| C4 | A `priority` ranking component or new reason codes change a frozen contract | **ADR + orchestrator** (`docs/CONTRACTS.md` line 6) | Phase 2A #1 route (b) |
| C5 | Eval corpus is inert under ADR-0027; 4 suites already red | **Corpus decision** (item 34) | Phase 2A #4 |
| C6 | Market graph is empty — 0 of 5,451 markets carry typed edges | **ASK before adding an agent node** (item 32) | knowledge §5.5 |
| C7 | Module items are unmeasurable (`link_events.plan_id` FK) | **ADR** — analytics identity model | knowledge §6 |
| C8 | Knowledge §5.5 weights would drop `semantic`/`graph`/`underlinked`/`historical` (0.52 of current weight) | **Owner decision** + `ranking_weights.v2.yaml` | knowledge §5.5 |

---

## 5. Work classified by type

**Policy / configuration only (no code):**
- `recommendation.v2.yaml` — page-family routing expansion (C1)
- `ranking_weights.v2.yaml` — any weight change (C8)
- `taxonomy.v4.yaml` — hub as source, expert family (C2, C3)

**Database:**
- priority registry table + audit columns (migration 0013)
- surface discriminator on plans; module-item identity for analytics (C7)
- severity on opportunities

**Service:**
- priority read path and post-gate ranking input
- hub membership computation
- reciprocity checks (§2.7)
- freshness fan-out (§2.9)
- the 8 missing audit classes (§2.10)

**Contracts (frozen — orchestrator approval):**
- `RANKING_COMPONENTS` additions (`priority`, `freshness`)
- new `ReasonCode` members for priority types and hub membership
- `ModuleKind` additions
- surface / action fields on `LinkPlan`

**UI:**
- 4 missing reviewer controls (§2.11)
- priority and hub explainability panels

**Analytics:**
- module-item attribution (C7)
- surface / intent / priority dimensions

**Evaluation:**
- corpus rebuild (C5) — coupled to C6

---

## 6. Proposed smallest safe Phase 2A vertical slice

**Slice 2A-1: the governed priority registry, shadow-only, no contract change.**

Chosen because it is the only Phase 2A deliverable not blocked by an owner decision, and because
it is *inert by construction*: it computes what priority would contribute without changing any
live score.

**In scope**
1. Migration 0013: `page_priorities` — page id, priority type (closed set of the 9 knowledge
   §5.1 kinds), tier/weight, reason, owner, `effective_from`, `effective_to`, `active`,
   `created_at`, `updated_at`, `created_by`, `updated_by`. Unique on (page, type) among active
   rows; CHECK that `effective_to > effective_from`.
2. A read path in `ken_services/recommendations/` resolving a page's active designations at a
   given instant (time-bound by construction — an expired designation simply does not resolve).
3. A **shadow report only**: for each candidate that *already passed every gate*, record what a
   priority contribution would have been. Written to a report artefact, not to `link_plans`, and
   never to the live `commercial` component.
4. Unit tests, including the load-bearing one: **a designation on a pair that fails the
   geography or market gate changes nothing**, asserted against `gates.py` directly rather than
   through the inert corpus.
5. Unit-level evaluation fixtures for priority ordering that construct their own pairs and so do
   not depend on the item-34 corpus.

**Explicitly out of scope** — each blocked or deferred:
- any change to `RANKING_COMPONENTS`, `ReasonCode`, `ModuleKind` or `LinkPlan` (C4)
- any live scoring change, any `ranking_weights.v2.yaml` (C8)
- page-family routing (C1), hubs (C2), expert family (C3)
- corpus-recorded fixtures (C5)
- API route and reviewer UI — deferred to 2B so the slice touches no frozen contract

**Acceptance criteria**
1. `alembic upgrade head` then `downgrade -1` round-trips cleanly; the downgrade expectation test
   is updated.
2. A designation that is inactive, not yet effective, or expired never resolves.
3. For every pair refused by `geography_gate` or `market_gate`, the shadow output is identical
   with and without designations — asserted, not asserted-by-absence.
4. Shadow output records, per candidate: page, priority types resolved, the hypothetical
   contribution, and the policy version that produced it.
5. `ruff` and `mypy` clean; `tests/unit` stays at 4,657 passed or higher with no new skips.
6. No row is written to `link_plans`, `page_modules` or any analytics table by this slice.

**Known limitations to document on completion**
- Explainability is limited until route (b) adds priority-specific reason codes (C4).
- The shadow output is not comparable across corpora while the eval corpus stays inert (C5).

---

## 7. Why not the other Phase 2A deliverables first

| Deliverable | Why it cannot start |
|---|---|
| #2 page-family routing | Item 29 reserves the decision to the owner, verbatim. Writing `recommendation.v2.yaml` without it would be inventing editorial policy. |
| #3 topic-hub membership | `hub.linkable_as_source: false` needs taxonomy v4; the corpus has 1 hub page; curation input does not exist; generating membership without curation is a knowledge §9 non-goal. |
| #4 evaluation fixtures | The corpus is inert (item 34). Recording fixtures now writes empty expectations — the exact defect item 34 refused to commit. |
| #5 shadow-mode output | Not a deliverable on its own; it is the delivery mode of 2A-1 above. |

---

## 8. Recommended sequence after 2A-1

1. **2A-1** — priority registry, shadow-only *(this slice, pending approval of §9)*
2. **Owner decision C1** → `recommendation.v2.yaml` routing expansion *(policy data, no code)*
3. **Decision C5** → eval corpus rebuild, which also unblocks items 30 and 34
4. **ADR for C7** → module-item analytics identity *(largest measurable win: ~10× the surface)*
5. **ADR for C4** → `priority` component + reason codes → `ranking_weights.v2.yaml`
6. **Decision C6 / item 32** → market graph → unblocks adjacency, hubs and knowledge §5.5
7. **2B** — surfaces, reviewer controls, audits, reciprocity, freshness fan-out

C7 is placed early deliberately: without it, Business Phase 2 cannot measure the surface that
carries most of its intended value, and knowledge §6 is unsatisfiable.

---

## 9. Decisions needed before implementation proceeds

Implementation has **not** started. These are ASK-level under `CLAUDE.md`:

1. **Approve slice 2A-1 as scoped in §6** (migration 0013 + read path + shadow report + tests),
   or redirect.
2. **C1 — page-family routing:** which of the 12 families may be sources, which targets. Item 29
   says this is yours. Evidence is ready (§2.3 table).
3. **C4 — frozen contract:** approve or refuse adding a `priority` ranking component and
   priority-specific reason codes. If refused, 2A-1 stays on route (a) permanently and
   explainability is limited.
4. **C5 — eval corpus:** rebuild now, or after the market graph (item 32)? Item 34 warns that
   doing it before C6 means doing it twice.
5. **C7 — analytics identity:** confirm an ADR should be raised for measuring module items.

Nothing in this document has changed implementation code, policy data, contracts or the database.

---

## 10. Owner decisions (2026-11-19)

| # | Decision | Effect on the sequence |
|---|---|---|
| D1 | **Approve 2A-1** as scoped in §6, local only. Ineligible pages earn no benefit; priority never bypasses any gate; a load-bearing test must prove a designation cannot make a gate-failing pair survive; shadow output only. | Implemented — §11 |
| D2 | **Page-family routing (C1):** report, survey, benchmark, insight, case_study, pov, consulting, hub and brand_comparison may be sources AND targets subject to the gates; corporate, transactional and other are excluded by default. Eligibility, not equal ranking — journeys that help the visitor are preferred. | A *proposed* `recommendation.v2` with its own tests, NOT activated and NOT part of 2A-1 |
| D3 | **Priority gets its own `priority` ranking component (C4)**, never permanently fed through `commercial`. ADR to cover versioned reason codes, contract migration, backward compatibility, shadow comparison, post-gate proof and a dominance limit. | ADR draft next; no frozen-contract change until accepted |
| D4 | **Eval corpus (C5):** full rebuild only AFTER the market graph (item 32). 2A-1 gets a small purpose-built fixture set; no empty expectations. | Item 34 stays open |
| D5 | **Module analytics identity (C7):** raise an ADR treating module-item identity as first-class rather than forcing interactions into fake `link_plans`. | ADR draft after D3's; no schema change until reviewed |
| D6 | **Topic hubs (C2):** no auto-generated public hubs or doorway pages. Design curated membership and explanation; hubs may be sources and targets in the proposed routing; shadow only. | Pending curated business input |
| D7 | **Expert family (C3):** do not invent `expert` or map experts to `other`. Record it as a taxonomy-v4 requirement. | Pending expert-page inventory, URL patterns and ownership |

---

## 11. Slice 2A-1 — verified result (2026-11-19)

**Built.** Migration `0013_page_priorities` after confirming the head was `0012_page_modules`
(single linear chain). Vocabulary in `ken_core/priority.py`; domain resolver
`recommendations/priority.py`; shadow `recommendations/priority_shadow.py`; read path and
add/retire/list management `recommendations/priority_store.py`; CLI
`python -m ken_services.recommendations.priority_cli`. Nothing in `ken_contracts`, any policy
file, any live score, plan, module or analytics row changed.

**Two design decisions made during implementation, both worth reviewing:**

1. **Which candidates may enter a shadow.** Survivors, plus candidates cut ONLY by
   `per_source_limit` / `diversity_limit` — these cleared every gate and both floors and lost a
   slot to higher-ranked candidates, which is precisely where priority could legitimately change
   the outcome. `below_min_relevance`, `below_min_score` (D1: never bypass relevance) and
   `link_density_exceeded` (a safety guard, not a capacity race) are excluded in SQL. This rests
   on the pipeline order gates → min_relevance → placement → min_score → caps, read from
   `pipeline.py`, not assumed.
2. **No in-place edit.** The management surface offers add and retire only. An edit would
   overwrite `updated_by` and lose the prior state; retire-and-add keeps both rows, so the row
   set is the audit history. This also avoids needing new `EventName` members, a frozen
   vocabulary.

**Two lifecycle defects found in owner review and fixed before acceptance (2026-11-19).** Both
came from modelling the lifecycle as a BOOLEAN rather than as an interval:

| | Defect | Fix |
|---|---|---|
| 1 | `active=false` made a retired designation resolve as absent **for every past date**, so a shadow report reconstructed for a date on which it was genuinely in force was wrong — defeating the point of resolving designations at an `--at` instant | Retirement records an INSTANT. Validity is `[effective_from, min(effective_to, retired_at))`, so the designation still resolves before its retirement and not at or after it |
| 2 | `UNIQUE (page_id, priority_type) WHERE active` forbade a **scheduled replacement** (two rows of one type, disjoint in time) and let an **expired but still-active** row block its own successor | The rule is non-overlap, not uniqueness: a trigger refuses two designations of one (page, type) whose validity intervals overlap. Adjacent half-open intervals are accepted |

A future `retired_at` now schedules a handover, so an open-ended designation is replaced on a
date without editing it. `retired_at` at or before `effective_from` gives an empty interval: in
force at no instant, blocking nothing. The same arithmetic exists twice — `ken_priority_validity`
in SQL (what may be STORED) and `validity_end` in Python (what a report SEES) — and a
parametrised test holds them to each other over seven cases, because a divergence would let the
database accept a pair the report treats as overlapping, or the reverse.

**No `CREATE EXTENSION` was added.** A GiST exclusion constraint would express non-overlap
declaratively but needs `btree_gist`; `tstzrange` is core PostgreSQL, so a trigger plus
`pg_advisory_xact_lock` (both established patterns here) gives the same guarantee with no new
deployment prerequisite. Every migration in this chain states that it creates no extension, and
ADR-0017 removed the only one this project ever had.

**Decision 4 fixture coverage** — all thirteen required cases, built without the inert corpus:
active, expired, inactive, future-dated, protected, gone/stale (plus not-published, noindex,
non-canonical, HTTP-error), geography-gate failure, market-relevance failure, page-family
failure, eligible pair moving only in shadow, ties, conflicting/overlapping designations, and
determinism across repeated runs with reversed input order.

**Evidence.** Unit + contract **5,269 passed**, 32 skipped; unit 4,768 (was 4,657) and
contract 533. Full integration **1,293 passed, 4 failed, 5 skipped** — the four failures are
exactly item 34's known-red list by name, unchanged by this slice. New: 69 unit and 58 integration tests. Migration round trip and all seven
"a downgrade removes exactly these" tests pass; ORM and live database agree on every CHECK name
and both indexes; the SQL and Python validity intervals agree over seven cases. On a REAL engine
run with every page designated tier 1, the report holds exactly the 8 qualified candidates and
none of the 12 `market_unrelated` refusals, and every live table is identical before and after.
On a real run with `max_plans_per_source = 1`, designated capacity-cut candidates are swapped
into the shadow plan set while each source's plan count is preserved. CLI smoke: max priority
share **0.140** at the provisional 0.10 weight.

Known limitations and two out-of-scope findings (integration tests silently skipping on this
machine; repo lint red since the 2026-11-17 archive commit) are recorded in `NEXT_STEPS.md`.
