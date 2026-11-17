# Session handover — 2026-11-17

- **Status:** current as of 2026-11-17. Written immediately before a Claude subscription account
  switch, so a fresh session with no conversation history can pick the work up.
- **Read first:** `CLAUDE.md`, `START_HERE.md`, `PRD.md`, `TRD.md`, `AUTONOMY_AND_PERMISSIONS.md`,
  `IMPLEMENTATION_PLAN.md`. This document does not replace them; it records what is *not*
  derivable from the code or the git history.
- **Contains no secrets.** CMS tokens live in your local `.env` only.

## 1. The single most important distinction

**kenresearch.com is the production target. thekenconsulting.com is a disposable test site.**

This is not stated anywhere in the code and is easy to get wrong:

- **kenresearch.com** — the real site this system is being built for. So far only *read-side*
  work exists against it: sitemap crawl, content inventory, taxonomy derivation (ADR-0025).
  **There is no write-capable CMS adapter for kenresearch.com.** Building one is the next real
  milestone.
- **thekenconsulting.com** — an unrelated site used purely as a live test harness.
  `services/ken_core/providers/cms/kenconsulting.py` (ADR-0028) exists *only* to prove the
  apply / validate / rollback / revalidate cycle works against a real API. It is not production
  code and carries no special status once a real kenresearch.com integration exists.

Findings from the kenconsulting live test (below) are quirks of *that* site. Do not assume they
apply to kenresearch.com.

## 2. Live test performed on thekenconsulting.com

Full records, including every deployment id, are in
[`docs/live-tests/kenconsulting-2026-11/`](../live-tests/kenconsulting-2026-11/):

- `applied.json` — the seven bulk-applied links with deployment ids and before/after context.
- `reports.json` — the seven test reports and their fields.
- `plan.json` — the candidate anchors considered.
- `scripts/` — the exact scripts used (read tokens come from `.env`, never hardcoded).

### What was pushed, and what is still live

| Deployment | Page (slug) | Field | Anchor | Target | State |
|---|---|---|---|---|---|
| 6 | united-arab-emirates-regional-auto-components… | `v02_market_overview` | (first attempt) | qatar-tire-distribution… | **rolled back**, page clean |
| 9 | united-arab-emirates-regional-auto-components… | `v02_market_overview` | "Qatar tire distribution market" | qatar-tire-distribution… | **live** (approved, left deliberately) |
| 10 | thailand-ev-charging… | `v02_faq` | "two-wheeler" | malaysia-electric-two-wheeler… | **live** (test) |
| 11 | philippines-automotive-lubricants… | `v02_regional_analysis` | "two-wheeler" | malaysia-electric-two-wheeler… | **live** (test) |
| 12 | united-arab-emirates-regional-auto-components… | `v02_market_trajectory` | "charging infrastructure" | thailand-ev-charging… | **live** (test) |
| 13 | qatar-tire-distribution… | `v02_market_trajectory` | "fleet management" | indonesia-commercial-vehicle-leasing… | **live** (test) |
| 14 | indonesia-commercial-vehicle-leasing… | `v02_market_segmentation` | "ride-hailing" | united-arab-emirates-mobility-as-a-service… | **live** (test) |
| 15 | malaysia-electric-two-wheeler… | `v02_market_overview` | "charging infrastructure" | thailand-ev-charging… | **live** (test) |
| 16 | united-arab-emirates-mobility-as-a-service… | `v02_market_overview` | "fleet availability" | indonesia-commercial-vehicle-leasing… | **live** (test) |

**Eight links are still live on the test site** (9 plus 10–16). Deployments 10–16 were chosen by
hand to exercise the rendering path, **not** by the engine — several would fail the real ADR-0027
geography/market gates. They should be rolled back once the frontend team has seen them.
`deployment_id` is all that is needed; see `scripts/rollback_6.py` for the call shape.

Note: the UAE auto-components page also carries a manual test link added by Snehal (anchoring the
word "automotive"), which is not ours.

### Findings — all on the CMS/frontend side, none in our engine

1. **BLOCKER — body content renders as plain text, not HTML.** An `<a>` tag reaches the page
   correctly and is present in the API payload, but displays to the reader as escaped text
   (`&lt;a href="…"&gt;`). This makes the feature invisible and worth nothing for SEO.
   Reproduced on every page tested.
2. **One revalidation call does not refresh all origin instances.** `POST /api/revalidate` with
   `{"slug": "...", "type": "v0.2"}` updated only some instances; the same page served old and new
   content across repeated polls. Roughly six calls were needed to converge.
3. **Rollback holds a DB row lock and returns HTTP 500.** `Lock wait timeout exceeded`; succeeded
   only on the fifth attempt, ~2.5 minutes later. Our client timeout (10s) is shorter than their
   write, which compounds this.
4. **`DEBUG = True` on a public domain** — leaks framework/Python versions, server filesystem
   paths (`/home/ubuntu/ken_backend/…`) and installed apps.
5. **`X-Robots-Tag: noindex, nofollow` served site-wide** — if it survives launch, interlinking
   delivers zero SEO value.

An earlier misdiagnosis is worth recording so it is not repeated: the first conclusion was that
the site strips `<a>` tags. It does not. The tags survive; they are escaped at render. The
original count that suggested stripping came from inline tags belonging to *other* reports'
`product_brief_detail` blocks, not the field under test.

## 3. Agreed editorial placement rules (owner-confirmed 2026-11-17)

These confirm the existing code; **no change was required**.

- **Never** insert into the Table of Contents or the left-hand navigation / Overview panel.
  (Note: the *Market Overview body section* IS allowed — the exclusion is the left nav panel.)
- **Never** insert into Research Methodology — boilerplate, no editorial value.
- **All other body sections are in scope**: Market Overview, Scope of Report, Market Trajectory,
  Market Breakdown, Market Segmentation, Regional Analysis, Growth Drivers, Competitive Landscape,
  Key Stakeholders, FAQ — and equivalents on any other product/collateral page type.
- **Within an allowed section, links land only in editorial prose** — complete sentences in
  running text. Never a bullet, heading, segmentation label (e.g. "S7: Ride-Hailing &
  App-Based Taxi") or table cell.

Enforced by `EXCLUDED_FIELDS` in `infrastructure/scripts/export_kenconsulting.py:59` and
`block_is_prose` in `services/ken_services/recommendations/placement.py:591`.

**Scope decision:** interlinking will be implemented **site-wide on kenresearch.com, on any
product or collateral page type.** Market research reports are the first page family, but the
contract must not be built report-specific.

## 4. Known structural limit — not a bug

Running the engine over the seven-report corpus, several pages produce no proposal at all, with
refusal reason `no_market_keyword_anchor`: no other report's market is named anywhere in their
prose. The UAE MaaS report is the clearest case — none of the other six markets appears in its
body text. This is the structural reason the related-reports module (item 31) exists: some pages
can only be linked through a module, never through an in-prose anchor.

## 5. Git state — READ THIS BEFORE PUSHING

The last account switch caused a branch fork that cost real time. Current layout on
`github.com/abhinav108737-automation/Ken-Interlinking-OS`:

- `main` — stale, still at the Phase 0 commit (`55aa14c`). Untouched.
- `phase-1-content-inventory` — **a different, older line of work** (29 commits) ending in
  "docs: handoff notes for continuing on another machine". Contains SSRF guard, Qdrant wiring,
  Google Analytics auth, and a kenresearch.com crawler pilot.
- `phase-1-content-inventory-local-20261116` — **the current working line** (this machine's local
  branch, pushed 2026-11-16). This is where active work lives.

The two branches **diverged from a common ancestor** (`c404769`) and both independently rebuilt
the same Phase 9 autonomy / Qdrant / learning work. A merge produces 70+ conflicts, mostly
*add/add* on whole files (`apps/web/lib/autonomy.ts`,
`services/ken_services/learning/promote.py`, `shadow.py`, whole test files, whole eval datasets).

**Do not auto-resolve this.** It needs a human decision about which implementation is canonical.
Both branches are preserved on the remote precisely so that review can happen deliberately.
The local branch was pushed under a new name rather than force-pushing, so nothing was lost.

## 6. Open items

1. **Reconcile the two diverged branches.** Needs a human call per file family.
2. **Send the requirements to Sachin / raise the Jira ticket** — the frontend requirements
   (R1–R8, acceptance criteria) were drafted in-session. R1 (render as HTML) is the blocker;
   R2 (href format: site-relative vs absolute) needs a decision from their side.
   Sachin's reply email had not been read at the time of writing.
3. **Roll back deployments 9–16** on thekenconsulting.com once the frontend team has seen them.
4. **Build the write-capable CMS adapter for kenresearch.com.** Nothing exists yet. The engine is
   CMS-agnostic, so this is a new adapter alongside `kenconsulting.py`, not an engine change.
5. **`policies/taxonomy/taxonomy.v3-demo.yaml`** — committed in this session for safekeeping.
   It is **TEST ONLY** (adds the seven demo reports' markets plus a shared
   `automotive-aftermarket-distribution` parent). It is not the production taxonomy.
6. **Basecamp** — the team-facing tracker is the "Marketing Automation Team" project; the work
   appears there as the "Backlink Automation / Agentic Backlink Outreach System" list. Project
   description and to-do structure were drafted in-session.

## 7. Standing constraints that bit during this session

From `CLAUDE.md`, reproduced because they were load-bearing here:

- Touching production CMS data, using real CMS write credentials, and pushing to remote git are
  all **ASK** actions. `KEN_CMS_WRITES_ENABLED` must be explicitly set for any write.
- Every mutation must be idempotent, version-aware, auditable and reversible — this is why every
  link above has a recoverable `deployment_id`.
- Do not silently modify architecture decisions; raise an ADR or ask.
