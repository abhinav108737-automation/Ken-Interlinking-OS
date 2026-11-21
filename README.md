<div align="center">

# Ken Research Agentic Interlinking OS

**A governed, self-hosted system that discovers, reviews, publishes, verifies and learns from contextual internal links across Ken Research content.**

![Status](https://img.shields.io/badge/software%20phases-0--9%20implemented-2ea44f)
![Python](https://img.shields.io/badge/python-3.12-3776ab)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-336791)
![Qdrant](https://img.shields.io/badge/vector%20index-Qdrant-dc382d)
![Inference](https://img.shields.io/badge/inference-local%20Ollama-black)
![Control](https://img.shields.io/badge/publishing-human%20governed-orange)

</div>

## Executive summary

Ken Research has thousands of reports, articles, surveys and commercial service pages. This project turns that catalogue into a connected research journey.

The system:

1. reads and versions website content;
2. understands each page's market, geography, industry, type and intent;
3. finds genuinely related source and destination pages;
4. applies hard relevance, geography, eligibility and safety gates;
5. drafts a natural contextual link and explains why a reader may use it;
6. shows the exact source evidence, destination evidence, anchor and placement to a reviewer;
7. records approval, revision or rejection and the reason;
8. publishes only an explicitly authorised, version-locked change through the CMS API;
9. verifies the link in both CMS data and the public rendered page;
10. keeps a rollback handle and learns from decisions and outcomes.

The business objective is not to maximise link count. It is to help visitors discover the next useful piece of research, reduce orphan and dead-end pages, strengthen coherent topic clusters, and create relevant journeys toward reports, samples, analysts, surveys, experts and consulting services.

> **Current safety boundary:** the latest writes are controlled canaries on `thekenconsulting.com`, a test host that declares `noindex,nofollow`. They prove drafting, approval, CMS mutation, public rendering and rollback. They do **not** transfer SEO authority and are not a claim of production impact on `kenresearch.com`.

## What has been built

| Capability | What it does | Current state |
|---|---|---|
| Content inventory | Imports exports or crawls public pages; normalises URLs; stores immutable page versions, canonical data and existing links | Implemented |
| Content intelligence | Classifies page type, market, industry, geography and intent; creates embeddings and typed relationships | Implemented |
| Hybrid discovery | Uses semantic, lexical, taxonomy, geography, graph and under-linking signals | Implemented |
| Recommendation engine | Applies deterministic gates, scoring, anchor selection and placement rules; creates explainable link plans | Implemented |
| Contextual copy drafting | Links existing text, makes a supported micro-edit, or adds a short evidence-backed bridge sentence | Implemented and live-tested |
| Human review | Shows before/after copy, evidence, seven KPI scores and hard gates; captures approve/revise/reject feedback | Implemented |
| Controlled CMS publishing | Dry run, exact version lock, idempotent operation ID, atomic apply, verification and rollback | Implemented and live-tested |
| Related-market and regional clusters | Evaluates exact-market regional siblings and closely connected workflow/topic pages without forcing reciprocity | Implemented in review and canary workflows |
| Graph intelligence | Finds orphans, dead ends, inbound/outbound connections and paths; supports snapshot, diff and sitemap seed audits | Implemented |
| Priority routing | Holds governed, time-bound business-priority designations behind every relevance and safety gate | Implemented in shadow mode |
| Analytics and learning | First-party link events, optional GA4/GSC inputs, decision logs, evaluation datasets and policy/skill proposals | Implemented; real outcome volume is still limited |
| Selective autonomy | Supports shadow, guarded and autonomous policy modes; production promotion remains human controlled | Implemented; default is shadow |

## Contextual interlinking, in plain language

This is the key capability currently being piloted.

Keyword overlap alone is not enough. A good contextual link must make sense inside the sentence, preserve the source report's meaning, and tell the visitor what additional information the destination provides.

### The reader test

A proposal is eligible only when the surrounding copy answers both questions:

1. Why is this destination related to the point I am reading?
2. What useful information will I receive if I click?

If either answer is unclear, the system creates no inline link.

### The four permitted outcomes

1. **Link useful text that already exists.** This is the lowest-risk option.
2. **Make a small contextual edit.** Add only a supported market term or qualifier without changing the claim.
3. **Add a short bridge sentence.** Explain the operational or research connection using evidence from the source and destination.
4. **Create no link.** A missing link is better than a forced, misleading or promotional link.

### Example implemented on the test host

Source topic: Kuwait cloud-based retail inventory forecasting.

Destination topic: Kuwait cloud-based order management systems.

Published contextual copy:

> For execution, **order management systems** help turn inventory forecasts into stock reservations and fulfilment plans. The Kuwait market is projected to reach USD 137 Mn by 2032.

Why this works:

- the source already discusses forecast-led inventory decisions;
- order management is the next operational step in that workflow;
- the anchor accurately predicts the destination;
- one publicly visible destination figure gives the visitor a useful preview;
- the wording remains neutral and useful even without the hyperlink;
- the original source facts and meaning are preserved.

### Placement rules learned from real rendering

Preferred surfaces are visible explanatory prose in:

- Market Trajectory;
- Growth Drivers or Opportunities, but only when that content visibly renders;
- segmentation explanation;
- regional analysis, but only when that content visibly renders.

Hard exclusions:

- **Market Overview:** it must remain focused on the report's own market;
- **Related Reports:** it already performs the recommendation job, so an inline link there is duplicative and pushy;
- headings, tables, navigation, table of contents, citations, methodology, legal copy, hidden descriptions and boilerplate FAQs.

Live testing exposed an important template issue: many CMS fields exist but do not appear on the public page. A CMS success is therefore never treated as proof. Every applied link must also be found as a visible, crawlable `<a href>` in rendered HTML. Non-rendering placements are rolled back.

### Copy controls

- Preserve existing numbers, dates, entities, comparisons and market claims.
- Every added claim must be traceable to stored source or destination evidence.
- Use neutral analyst language, normal punctuation and no sales pressure.
- Do not use “click here”, curiosity bait or unsupported cause-and-effect wording.
- Avoid em dashes and repetitive AI-style phrasing.
- Use no more than one new inline link per content block.
- Inserted bridge copy is governed separately from the internal rationale and cannot simply publish the rationale as page text.
- Current bridge-copy rule: two short sentences, 32–38 words, rounded currency figures and no direct address to the visitor.

Detailed editorial rules: [Full contextual interlinking](knowledge/FULL_CONTEXTUAL_INTERLINKING.md) and [optimisation lessons](knowledge/INTERLINKING_OPTIMISATION_LESSONS.md).

## Reviewer dashboard and scoring

The local v0.2 review dashboard runs at `http://127.0.0.1:4177`:

```powershell
npm run dashboard:v02
```

Each proposal shows:

- source and destination pages;
- current source copy and proposed linked copy;
- exact anchor and placement section;
- source and destination evidence;
- reader benefit and relationship rationale;
- current CMS version and validation state;
- indexability, canonical and rendered-link gates;
- approval history, structured rejection reason and reviewer notes;
- live deployment status after publishing;
- a filterable topic graph with existing, proposed and live edges.

The editorial reference score is out of 100:

| KPI | Weight | What the reviewer checks |
|---|---:|---|
| Reader need | 15 | The source creates a genuine question, task or useful next step |
| Source evidence | 15 | The exact source passage establishes that need |
| Destination fit | 20 | Destination evidence answers or materially advances it |
| Anchor expectation | 15 | The anchor accurately predicts the page that opens |
| Context and placement | 15 | The link reads naturally in an approved visible section |
| Added information value | 10 | The destination adds information instead of repeating the source |
| Quality and risk | 10 | The wording is supported, neutral, non-redundant and non-promotional |

Reference outcomes are 85–100 **approve**, 70–84 **revise**, and 0–69 **reject**. Missing evidence produces **insufficient evidence**, regardless of the numeric total.

This is an editorial aid, not a Google score, ranking prediction or LLM-citation score. A high score cannot bypass hard gates.

## Hard gates before a link can go live

```text
Candidate
  -> source and destination eligible
  -> page families compatible
  -> market relationship valid
  -> geography relationship valid
  -> source evidence exists
  -> destination evidence exists
  -> approved visible placement
  -> natural, descriptive anchor
  -> no duplicate or over-linking
  -> human decision where required
  -> CMS dry-run validates
  -> source version still matches
  -> atomic apply succeeds
  -> CMS content verifies
  -> public rendered anchor verifies
  -> deployment and rollback recorded
```

Business priority, commercial value and orphan status can reorder candidates only **after** these gates pass. They cannot make an irrelevant link eligible.

## Current canary evidence

The repository contains durable, reproducible audit files for the v0.2 test-host work.

### Kuwait topic cluster

- Seven evidence-backed proposals were drafted and scored from 92 to 97.
- Six owner-approved proposals are CMS-verified and publicly rendered.
- Deployment history: canary revisions `35–37`, approved batch `38–42`.
- The seventh proposal remains unpublished because it was not approved.
- The graph snapshot contains 28 reports and 12 observed/proposed connections, including six live topic links.

### Exact-market regional clusters

- Twelve approved exact-market regional links were published as deployments `43–54`.
- Owner feedback identified overly formulaic wording even though technical gates passed.
- All twelve sentences were rewritten in a more natural analyst voice as deployments `55–66`.
- This learning is enforced centrally by the copy-rule validator and shown in the dashboard before approval.

### Earlier v0.2 contextual batch

- Four approved contextual links rendered successfully.
- Three technically successful CMS changes did not appear in the public template and were rolled back.
- That result established rendered visibility as a mandatory post-apply gate.

Key records:

- [Contextual publishing events](knowledge/contextual-interlinking-publish-events.jsonl)
- [Approved Kuwait batch](outputs/contextual-v02-live/approved-topic-batch-2026-11-20.json)
- [Kuwait canary](outputs/contextual-v02-live/topic-kuwait-1-live-2026-11-20.json)
- [Optimisation lessons](knowledge/INTERLINKING_OPTIMISATION_LESSONS.md)
- [GitHub-pattern implementation report](docs/handover/GITHUB_REPOSITORY_INHERITANCE_IMPLEMENTATION_2026-11-20.md)

## System architecture

The core rule is:

```text
Model suggestion
  -> typed structured output
  -> deterministic policy and validation
  -> human or policy authorisation
  -> deterministic CMS execution
  -> rendered verification
  -> measurement and learning
```

Models may propose or critique. They never receive direct production database or CMS authority.

```mermaid
flowchart LR
    SRC[Exports / public crawl / CMS read API] --> INV[Versioned content inventory]
    INV --> INT[Taxonomy, geography, intent, embeddings]
    INT --> RET[Hybrid candidate retrieval]
    RET --> GATE[Deterministic gates and scoring]
    GATE --> DRAFT[Evidence-backed contextual drafter]
    DRAFT --> REVIEW[Human review dashboard]
    REVIEW -->|approved| CMS[Controlled CMS adapter]
    CMS --> VERIFY[CMS + public DOM verification]
    VERIFY --> ANALYTICS[Events, outcomes and graph audit]
    ANALYTICS --> LEARN[Offline evaluation and proposals]
    LEARN --> GATE
    REVIEW -->|revise / reject| LEARN
```

### Technology

| Layer | Implementation |
|---|---|
| Control UI | Next.js, React and TypeScript |
| Contextual review UI | Local Node dashboard with evidence cards and interactive SVG graph |
| API | Python 3.12, FastAPI, Pydantic |
| Business services | Deterministic Python services for inventory, intelligence, recommendations, review, deployment, analytics and autonomy |
| Agent orchestration | LangGraph supervised workflow |
| System of record | PostgreSQL 16 with SQLAlchemy and Alembic |
| Vector retrieval | Qdrant derived index; PostgreSQL remains authoritative |
| Local inference | Ollama through a provider abstraction |
| Deployment | Typed CMS provider with validate/apply/verify/rollback lifecycle |
| Infrastructure | Docker Compose; services bind to loopback by default |

Dependencies flow inward:

```text
UI/API -> services -> core platform -> frozen contracts
```

## Software phases completed

| Phase | Delivery |
|---:|---|
| 0 | Contracts, platform, health checks, local stack and mock providers |
| 1 | Content inventory, crawling, parsing, canonical identity and immutable versions |
| 2 | Taxonomy, entities, embeddings, graph relationships and eligibility |
| 3 | Hybrid recommendations, deterministic scoring, placement and explanations |
| 4 | Supervised local-model review agents with typed state and failures |
| 5 | Authentication, RBAC, review inbox and structured feedback |
| 6 | Dry run, version locking, idempotent publishing, verification and rollback |
| 7 | First-party analytics plus optional read-only GA4 and Search Console providers |
| 8 | Evaluation datasets, experiments, skill/policy lifecycle and learning proposals |
| 9 | Shadow/guarded/autonomous decision policy and Qdrant vector architecture |

Each phase has a report at the repository root: `PHASE_0_REPORT.md` through `PHASE_9_REPORT.md`.

## Business Phase 2

Business Phase 2 is separate from software Phase 2. Its purpose is to strengthen engagement-oriented silo and priority routing without weakening editorial safety.

Implemented foundation:

- a governed page-priority registry with type, tier, reason, owner and effective dates;
- non-overlapping time validity and retirement history;
- shadow reporting that proves priority cannot bypass gates;
- graph auditing for orphans and dead ends;
- contextual topic and regional-cluster pilots;
- real reviewer decisions stored as evaluation data;
- reciprocal-rank-fusion utilities for future shadow comparisons.

Still governed or pending:

- production page-family routing expansion;
- curated public topic hubs;
- analytics identity for related-module items;
- production indexability/canonical mapping;
- enough positive, negative and click outcomes to train a real reranker.

The reranker readiness gate currently refuses training because the first topic judgment set has 6 decisions, 0 negative labels and 0 measured click outcomes. The minimums are 100 decisions, 20 negatives and 50 measured outcomes. Synthetic clicks are not accepted as proof.

See [Business Phase 2 knowledge](knowledge/BUSINESS_PHASE_2_PRIORITY_INTERLINKING.md) and [gap analysis](docs/handover/BUSINESS_PHASE_2_GAP_ANALYSIS.md).

## Graph and ranking improvements

Patterns from three permissively licensed reference projects were studied, then reimplemented inside the existing architecture rather than imported as a second engine.

Delivered:

- sitemap and sitemap-index seed discovery;
- graph snapshot and before/after diff;
- shortest-path queries;
- orphan, inbound and outbound summaries;
- reviewer graph filtering and clickable nodes;
- deterministic reciprocal-rank fusion;
- precision@k and recall@k helpers;
- a real-decision judgment set and reranker-readiness guard.

RRF remains shadow-only until real lexical and semantic rank exports can be evaluated against reviewer decisions. No external repository's synthetic performance claim is presented as a Ken Research result.

## Safety, governance and auditability

- PostgreSQL is the authoritative record; Qdrant is rebuildable derived state.
- Every content mutation has an operation ID and expected source version.
- Stale versions fail instead of overwriting newer editorial work.
- Protected, unpublished, noindex, redirected, non-canonical, stale and ineligible pages are blocked.
- Every apply is validated first and retains rollback metadata.
- Public rendered verification is mandatory after an apply.
- Roles are `viewer`, `reviewer` and `operator_admin`; no account is created by default.
- Models use narrow typed tools, never raw infrastructure credentials.
- Learning can propose a policy or skill but cannot promote it.
- Secrets, CMS credentials and large regenerable exports are excluded from Git.
- Architecture changes are recorded in 31 ADRs under [docs/adr](docs/adr/).
- Database evolution is recorded in 13 Alembic migrations.

## Quick start

### Prerequisites

- Python 3.12
- Node.js 22.22+ or 24.15+
- Git
- Docker Desktop
- Ollama

### Windows PowerShell

```powershell
git clone https://github.com/abhinav108737-automation/Ken-Interlinking-OS.git
cd Ken-Interlinking-OS

py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\pip.exe install -e ".[dev]" -c requirements.lock.txt
npm ci

Copy-Item .env.example .env
(Get-Content .env) -replace 'localhost','127.0.0.1' | Set-Content .env

docker compose up -d postgres qdrant
.venv\Scripts\alembic.exe upgrade head
ollama pull nomic-embed-text
```

Run the API:

```powershell
.venv\Scripts\uvicorn.exe ken_api.main:app --host 127.0.0.1 --port 8000
```

Run the main web application in another terminal:

```powershell
cd apps\web
$env:NEXT_TELEMETRY_DISABLED = "1"
..\..\node_modules\.bin\next.cmd dev --hostname 127.0.0.1 --port 3000
```

Run the contextual-review dashboard:

```powershell
npm run dashboard:v02
```

Open:

| URL | Purpose |
|---|---|
| `http://127.0.0.1:3000/inventory` | Pages, versions and existing links |
| `http://127.0.0.1:3000/intelligence` | Page classification and relationships |
| `http://127.0.0.1:3000/recommendations` | Inline link plans and score breakdowns |
| `http://127.0.0.1:3000/recommendations/modules` | Same-market regional module proposals |
| `http://127.0.0.1:3000/agents` | Agent runs and model steps |
| `http://127.0.0.1:3000/jobs` | Pipeline jobs and counters |
| `http://127.0.0.1:4177` | Contextual evidence and approval dashboard |
| `http://127.0.0.1:8000/docs` | Interactive API reference |

Create a local administrator:

```powershell
.venv\Scripts\python.exe -m ken_services.auth.cli user-create --username you --role operator_admin --display-name "Your Name"
```

Load the synthetic demo and run the non-publishing pipeline:

```powershell
.venv\Scripts\python.exe -m ken_services.inventory.cli import-file tests\fixtures\demo\research_site.jsonl
.venv\Scripts\python.exe -m ken_services.intelligence.cli analyze
.venv\Scripts\python.exe -m ken_services.recommendations.cli recommend
.venv\Scripts\python.exe -m ken_agents.cli agents
```

See [development instructions](docs/DEVELOPMENT.md) for macOS/Linux commands and full configuration.

## Verification

Run focused checks:

```powershell
.venv\Scripts\python.exe -m pytest tests\unit -q
.venv\Scripts\python.exe -m pytest tests\contract -q
.venv\Scripts\ruff.exe check .
.venv\Scripts\mypy.exe .
npm run lint
npm run typecheck
npm test
docker compose config --quiet
```

The repository also contains integration, migration round-trip, agent, evaluation and CMS-provider tests. Use `127.0.0.1` instead of `localhost` for the Windows PostgreSQL test URL; otherwise IPv6 resolution can cause integration tests to skip.

The latest added RRF unit suite passes 5/5. Historical full-suite evidence and known reds are recorded in [NEXT_STEPS.md](NEXT_STEPS.md) and the phase reports. Do not interpret a focused test result as a fresh full-suite certification.

## Repository map

```text
apps/web/                         Main Next.js control UI
apps/api/ken_api/                 FastAPI control API
agents/ken_agents/                Supervised LangGraph review agents
contracts/python/ken_contracts/   Frozen cross-boundary models
services/ken_services/            Deterministic business services
tools/v02-review-dashboard/       Contextual review and feedback dashboard
tools/v02-contextual-audit/       Evidence, drafting, validation and canary tools
tools/graph-intelligence/         Snapshot, diff, path and sitemap tools
tools/ranking-eval/               Judgments, readiness and RRF evaluation
policies/                         Versioned ranking, taxonomy, graph and autonomy policy
knowledge/                        Approved editorial and business learning
evals/                            Evaluation datasets, suites and results
migrations/                       Alembic schema history
docs/adr/                         Architecture decision records
docs/evaluation/                  Editorial and engine audits
docs/handover/                    Implementation and handover reports
tests/                            Unit, contract, integration and system tests
outputs/                          Reproducible shadow/live-test audit artifacts
```

## Known limitations and next decisions

1. **Not production-authority tested.** The canary host is `noindex,nofollow`.
2. **Rendered CMS coverage is incomplete.** Several authored fields are missing from the public template.
3. **Sitemap coverage is incomplete.** The staging sitemap exposed no usable report URLs during the latest audit.
4. **Taxonomy still needs business curation.** Market adjacency and company/product gazetteers remain incomplete.
5. **Reranking lacks outcome volume.** There are not enough rejections or measured clicks to train safely.
6. **Public topic hubs need curation.** The system must not auto-generate thin doorway pages.
7. **Module-item analytics identity needs an ADR.** Inline plans and module items currently have different measurement paths.
8. **Production CMS activation remains a separate approval.** Test-host access does not grant production authority.
9. **SEO results are not guaranteed.** Indexing, rankings, engagement and conversions must be measured through Search Console and first-party events.

The detailed, continuously maintained decision list is [NEXT_STEPS.md](NEXT_STEPS.md).

## Documentation path for reviewers and managers

Read in this order:

1. This README for the complete product view.
2. [START_HERE.md](START_HERE.md) for the original objective and constraints.
3. [Full contextual interlinking](knowledge/FULL_CONTEXTUAL_INTERLINKING.md) for current editorial behaviour.
4. [Optimisation lessons](knowledge/INTERLINKING_OPTIMISATION_LESSONS.md) for what the live tests taught us.
5. [Business Phase 2](knowledge/BUSINESS_PHASE_2_PRIORITY_INTERLINKING.md) for the engagement and silo strategy.
6. [NEXT_STEPS.md](NEXT_STEPS.md) for measured state, open decisions and limitations.
7. [ADRs](docs/adr/) for architecture rationale.
8. `PHASE_0_REPORT.md` through `PHASE_9_REPORT.md` for implementation history.

## Licence and provenance

This repository is proprietary Ken Research project code. Third-party libraries retain their own licences. Reference repositories are used for research and pattern comparison only; they are not vendored into the application. Any reused implementation must pass licence review and have documented provenance.

---

**Product principle:** create fewer, stronger links that help a person continue their research. Relevance, evidence and trust come before link volume or commercial priority.
