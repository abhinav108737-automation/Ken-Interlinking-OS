# Internal-linking reference repositories — provenance and research notes

- **Type:** research and provenance record. Not an ADR, not a dependency list.
- **Date:** 2026-10-16. **Governing rules:** CLAUDE.md #18 (no reference repositories in application
  source) and #19 (no copying without a licence check and documented provenance); `REPO_CLONE_PLAN.md`
  ("Licensing Rule"; prefer reimplementation of a pattern).
- **Scope:** five repositories the user named as *research references* for internal linking.

## 1. Provenance declaration (verified, not assumed)

**No code, data, model weight, prompt, notebook, configuration or text from any of the five
repositories below has been copied, adapted or vendored into this codebase.** None is a runtime,
build-time, test-time or optional dependency. Nothing was cloned into any application source folder,
and nothing was cloned at all while writing this document; `_research/repos/` does not exist in this
working tree. The influence is **conceptual only**: every mechanism here was specified in an ADR and
written from scratch.

Verification run before writing that paragraph, over the whole tree excluding `.git`,
`node_modules` and `__pycache__`:

```
grep -rni -E "webknograph|search-solved|searchsolved|seo-interlinking-tool|seo-monster|
  internal-link-mapper|avansaber|andersongrowth|martech-engineer|Deepak-J0shi" .
→ no matches (exit 1)
```

No `vendor/`, `third_party/` or `vendored/` directory exists, and no attribution or "adapted
from" / "copied from" comment anywhere in the tree refers to these projects (the only "derived from"
matches are unrelated prose about an actor derived from the session). This document is the first and
only mention of them in the repository.

## 2. Licence verification

Checked on 2026-10-16 through the GitHub REST API (repository metadata and, where present, the
`LICENSE` blob). No repository contents were downloaded or executed.

| Repository | Exists | Licence | Confidence |
|---|---|---|---|
| `martech-engineer/WebKnoGraph` | yes | Apache-2.0 | verified (API `license.spdx_id` = `Apache-2.0`) |
| `searchsolved/search-solved-public-seo` | yes | **CC BY-NC 4.0** | verified by reading the `LICENSE` file; GitHub classifies it as `NOASSERTION` / "Other" |
| `Deepak-J0shi/SEO-Interlinking-Tool` | yes | **none declared** | verified absent: no `LICENSE` file in the repository, API reports no licence. Default copyright applies — **not reusable** |
| `avansaber/seo-monster` | yes | MIT | verified (API `license.spdx_id` = `MIT`) |
| `andersongrowth/internal-link-mapper` | yes | MIT | verified (API `license.spdx_id` = `MIT`) |

Two consequences for this project, which is commercial and `license = "Proprietary"`:

- **`search-solved-public-seo` is non-commercial (NC).** Its code may not be reused here under any
  circumstances, including "small helper" reuse. It is readable as published technique only.
- **`SEO-Interlinking-Tool` has no licence**, so no permission to copy is granted at all. Same
  treatment: read, do not reuse.

Apache-2.0 and MIT would permit reuse with attribution and notice retention. That permission was
not exercised for any of them, so no NOTICE obligations arise.

## 3. The five references

### 3.1 WebKnoGraph (`martech-engineer/WebKnoGraph`, Apache-2.0)

**Problem.** Internal linking as a reproducible experiment: crawl a site, embed its content, build
the link graph, learn link predictions over it (GraphSAGE), and analyse the structural effect of a
proposed intervention (PageRank-style authority flow) *before* deployment. Delivered as Jupyter
notebooks with per-stage UIs.

**Concepts genuinely useful here.** (a) The site as an explicit directed link graph, with inbound
degree and authority distribution as first-class signals, not by-products. (b) Embedding-based
destination discovery as a retrieval stage separate from ranking. (c) *Pre-deployment* evaluation of
a link intervention — deciding whether a change is worth making before anyone edits a page.

**Not adopted, and why.** No graph database: Neo4j remains forbidden infrastructure (TRD §2;
ADR-0017 lifted only the Qdrant prohibition). The link graph is derived in SQL from `existing_links`
over current `page_versions`. No GNN: a learned link predictor is not deterministic, not explainable
from a stored record, and needs a training stack. ADR-0011 §4 instead fixes a transparent
`relevance = Σ weight × component` whose identity a Pydantic validator enforces, so every score is
re-checkable from the plan row alone. No notebooks in the product path: they are neither idempotent,
auditable nor reversible (CLAUDE.md #12).

**How the capability exists here.** Graph retrieval is the `graph` channel in
`services/ken_services/recommendations/channels.py` (under-linked targets in the source's industry,
fewest inbound links first, score `1/(1+inbound)`); inbound scarcity and complementarity are the
`underlinked` and `graph` components in `scoring.py`. Structural opportunities are ADR-0011 §7's
`orphan`, `underlinked` and `link_capacity` rows. Pre-deployment evaluation is the filter/score/
placement chain plus ADR-0012's agent critique and ADR-0013's reviewer gate — a human, not a model,
authorises the change (ADR-0011 §6, `TRANSITION_AUTHORITY`).

### 3.2 search-solved-public-seo (`searchsolved/…`, CC BY-NC 4.0 — read only)

**Problem.** A large collection of practitioner SEO scripts, including an anchor-text interlinker,
an anchor-text relevance checker and several semantic/SERP keyword-clustering tools.

**Concepts genuinely useful here.** (a) Anchor text must be *justified* by the destination's topic,
not merely match as a string — relevance checking as a step distinct from matching. (b) Keyword
clustering to group destinations by topic before choosing one.

**Not adopted, and why.** The NC licence is a hard stop for reuse (§2). Beyond that, one-shot CSV
scripts have no versioning, idempotency or audit trail, which CLAUDE.md #12 requires of every
mutation, and third-party SERP/keyword APIs are paid services (CLAUDE.md #1, #3).

**How the capability exists here.** `placement.py` enforces anchor justification deterministically:
anchors come only from the *target's* taxonomy aliases, title core or keyword bigrams, in that rank
order, copied verbatim from the source block, under the ADR-0011 §9.1 self-describing-anchor rule and
the geography-alias ban. Clustering is the owned taxonomy (ADR-0010) plus the `taxonomy` channel and
the lexical channel's `ts_rank_cd` over PostgreSQL full-text — no external keyword API.

### 3.3 SEO-Interlinking-Tool (`Deepak-J0shi/…`, no licence declared — read only)

**Problem.** Given a domain and one page: crawl, extract main content, score every other page with a
hybrid TF-IDF + SBERT similarity, extract keyphrases, and output ranked anchor → target suggestions
in a Streamlit app with CSV export.

**Concepts genuinely useful here.** (a) The hybrid of lexical and dense similarity: two retrieval
signals disagree usefully, and a union of both beats either. (b) Readability-style main-content
extraction before any scoring, so boilerplate does not drive similarity. (c) A per-page similarity
threshold the operator can tune.

**Not adopted, and why.** CSV suggestions cannot be version-aware or reversible; this project writes
`link_plans` whose content is immutable by DB trigger, with append-only transitions. A single blended
similarity number is not explainable, so ADR-0011 keeps components separate and in [0,1]. No
Streamlit: the reviewer surface is the authenticated Next.js console (ADR-0013). And placement is
never decided by similarity alone — a plan exists only if a natural anchor was found
(`no_natural_anchor`).

**How the capability exists here.** Lexical + dense hybrid = the `lexical` and `semantic` channels in
`channels.py`, unioned with per-channel scores recorded per candidate (ADR-0011 §2, §9.6 scale-free
normalisation). Main-content extraction is Phase 1's deterministic normaliser (`inventory/normalize.py`,
ADR-0009 §2). The tunable threshold is policy, not code: `selection.min_relevance` and
`selection.min_score` in `policies/recommendation/recommendation.v1.yaml`.

### 3.4 seo-monster (`avansaber/seo-monster`, MIT)

**Problem.** A local MCP server that answers SEO questions from the user's own Search Console, GA4,
PageSpeed and Cloudflare data, with read-first tooling, and reports crawl-depth distribution, orphan
pages, broken internal links and source → target link recommendations from high-in-degree pages.

**Concepts genuinely useful here.** (a) Orphan and crawl-depth reporting as standing site-health
metrics. (b) Link equity flowing from already-authoritative pages to pages that need it. (c) Tools
that are read-first and run locally on the user's own credentials. (d) Measuring whether a change
actually moved anything, against a control.

**Not adopted, and why.** No MCP server is added to this system: agents receive narrow typed tools
over the frozen `AgentBackend` protocol (ADR-0012), never a credentialled external client, and the
Phase 4 graph runs with egress refused. No live Google/Cloudflare API calls from the product path —
even where an API is free, calling an external service with production credentials is an explicit
ask (CLAUDE.md "You MUST ask before… calling any paid external API / sending repository data to an
external service"); ADR-0015 therefore builds on first-party capture plus GA4/GSC/CSV **import**.

**How the capability exists here.** Orphan and under-linked detection: ADR-0011 §7 `opportunities`.
Equity direction: the `commercial` and `underlinked` score components plus the `graph` channel's
ordering. Read-first tools: the five READ tools and one REVIEW tool of ADR-0012, enforced by a static
test. Measurement against a control: ADR-0015 §3 attribution, with `plan.id` as the stable link id.

### 3.5 internal-link-mapper (`andersongrowth/internal-link-mapper`, MIT)

**Problem.** A Claude skill: given a URL list, it proposes which page should link to which, the
anchor text, the paragraph to edit and a priority, covering new-post launches, orphan/dead-end
audits, pillar-and-spoke cluster architecture and anchor-text variation.

**Concepts genuinely useful here.** (a) A recommendation is actionable only if it names the exact
insertion point, not just the pair. (b) Every recommendation carries a stated reason. (c) Anchor
variation matters — one phrase repeated everywhere is a quality problem. (d) Pillar/spoke structure
as an intent for the link graph.

**Not adopted, and why.** This is the clearest case of the project's central constraint: the model
may not choose or write the anchor. A skill that emits anchor text can fabricate a phrase that is not
on the page. ADR-0011 §5 is deterministic and copies the anchor verbatim from a safe text run,
rejecting spans inside links, headings, tables, quotes, figures, code and citations, and rejecting
word-fragments of longer names; the alternatives section of ADR-0011 records that LLM-chosen
placement was considered and refused. Free-text rationale is likewise refused: `rationale` is
rendered deterministically from `reason_codes`. Per CLAUDE.md #16, a skill cannot promote itself into
this pipeline.

**How the capability exists here.** Exact insertion point: `PlacementLocator` — `block_kind`,
`occurrence_index`, `start`/`end` in normalised block text, `context_hash`, preview — relocated at
deploy time by (`context_hash`, anchor text, `occurrence_index`). Reasons: `reason_codes` +
`ScoreBreakdown` in `scoring.py`. Variation and over-linking control: `max_links_per_block`,
`max_internal_links_per_100_words`, `max_plans_per_source`, `max_open_plans_per_target`, plus the
one-open-plan-per-pair and per-(block, anchor) partial unique indexes. Cluster intent lives in the
taxonomy and the `page_type_targets` / `intent_compatibility` matrices.

## 4. Dependency-licence posture

Direct dependencies only, licences read from PyPI / the npm registry on 2026-10-16.

**Python runtime** (`pyproject.toml`): fastapi MIT · uvicorn[standard] BSD-3-Clause · pydantic MIT ·
pydantic-settings MIT · sqlalchemy[asyncio] MIT · alembic MIT · asyncpg Apache-2.0 · httpx
BSD-3-Clause · structlog MIT OR Apache-2.0 · pyyaml MIT · beautifulsoup4 MIT · langgraph MIT.

**Python dev:** pytest MIT · pytest-asyncio Apache-2.0 · pytest-cov MIT · ruff MIT · mypy MIT.

**Web** (`apps/web/package.json`): next, react, react-dom, tailwindcss, @tailwindcss/postcss, eslint,
eslint-config-next, vite, vitest, jsdom, openapi-typescript, @vitejs/plugin-react, the three
@testing-library packages and the three @types packages — all MIT; typescript Apache-2.0.

**Nothing copyleft, nothing unclear.** All of the above are MIT, BSD-3-Clause or Apache-2.0.
`requirements.lock.txt` (71 pinned packages) adds only transitive dependencies of these; re-check it
whenever the lock is regenerated. Two avoidances are already recorded in `pyproject.toml`:
`langgraph-checkpoint-postgres` is not installed because it pulls psycopg (LGPL), and beautifulsoup4
runs on the stdlib `html.parser` backend only (no lxml/html5lib). ADR-0017's `qdrant-client` is
Apache-2.0 (verified); neither it nor the Qdrant server is installed in this working tree yet. An
incompatible or unverifiable licence is a stop condition — raise it with the user.

## 5. What to cite

For the original ideas rather than this system's version of them, cite the sources, not this
repository:

- **Graph-based internal-link analysis, learned link prediction and pre-deployment intervention
  analysis** — WebKnoGraph (`github.com/martech-engineer/WebKnoGraph`, Apache-2.0), including its
  arXiv paper, which is the citable artefact.
- **Practitioner anchor-text interlinking and keyword/SERP clustering scripts** — Lee Foot,
  `github.com/searchsolved/search-solved-public-seo` (CC BY-NC 4.0; attribution required,
  non-commercial).
- **Hybrid TF-IDF + SBERT internal-link suggestion** — `github.com/Deepak-J0shi/SEO-Interlinking-Tool`
  (no licence declared). For the method itself, cite Sentence-BERT (Reimers & Gurevych, 2019) with
  classical TF-IDF retrieval.
- **Read-first, local, own-credentials SEO tooling and site-health metrics (orphans, crawl depth)** —
  `github.com/avansaber/seo-monster` (MIT).
- **Insertion-point-level link mapping and pillar/spoke cluster design as an assistant skill** —
  `github.com/andersongrowth/internal-link-mapper` (MIT).

The deterministic retrieve → filter → score → place design in this repository, its anchor-safety
rules and its immutable plan lifecycle are our own and are specified in
[ADR-0011](../adr/ADR-0011-recommendation-engine.md); the vector-layer split is
[ADR-0017](../adr/ADR-0017-qdrant-vector-architecture.md).
