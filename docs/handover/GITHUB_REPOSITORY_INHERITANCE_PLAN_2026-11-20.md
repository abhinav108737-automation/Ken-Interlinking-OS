# GitHub repository inheritance plan

Date: 20 November 2026

Repositories reviewed:

- `saizul/internal-link-graph` (MIT)
- `devbret/mapping-website-internal-links` (MIT)
- `sepfazeli/semantic-search-recommendation-engine` (MIT)

## Decision summary

Do not import any repository as a second engine. Ken already has deterministic PageRank, HITS,
crawl depth, orphan and bridge detection, PostgreSQL graph metrics, Qdrant semantic search,
lexical and semantic retrieval channels, a governed scorer, human review and controlled CMS
publishing. Replacing these would create duplicate systems and weaken the current controls.

Inherit only the missing patterns:

1. crawl snapshots, sitemap discovery, graph diffs, shortest paths and export formats;
2. an interactive graph view and page scorecard inside the existing reviewer;
3. reciprocal-rank fusion, real-feedback feature logging and offline ranking evaluation.

## Repository-by-repository fit

| Repository | Reuse | Do not reuse | Expected change | Planning efficiency estimate |
|---|---|---|---|---:|
| `internal-link-graph` | Sitemap seeding, resumable snapshots, crawl diff, shortest click paths, anchor distribution, GraphML/CSV export | PageRank, HITS, components, TF-IDF recommender and a separate crawler database | Better evidence for orphan, dead-end, redirect, noindex and canonical problems; before/after graph comparison | 40–60% less manual technical-audit time; 10–20% more discovery coverage where sitemap-only pages exist |
| `mapping-website-internal-links` | D3 interaction patterns, cluster filtering, node selection and page scorecards | Flask server, `links.json` as a second source of truth, direct Claude analysis and its crawler | Reviewers can see clusters, authority flow, weak pages and proposed edges instead of reading tables only | 25–40% faster reviewer triage; no direct ranking-quality gain |
| `semantic-search-recommendation-engine` | RRF fusion, NDCG/Recall evaluation structure, feature logging and a future real-click reranker | pgvector, FastAPI, SQLite vector store, synthetic click model and its reported synthetic gains | More stable candidate ordering and measurable retrieval quality; later, real engagement can train a reranker | 0–5% immediate quality gain; target 10–20% fewer irrelevant top-10 candidates after sufficient real labels |

The percentages are implementation planning ranges, not measured Ken outcomes. They must be
accepted only after a Ken baseline and controlled evaluation. They are not additive.

## Phase 1: destination-data drafting rule

Update the contextual drafter before graph work:

- Add one publicly visible destination fact when it directly helps the source reader.
- Prefer one market endpoint or one operational number, not several metrics.
- Record the exact destination passage and public-visibility check.
- Allow 21–35 words only when the destination fact is recorded; otherwise keep the 20-word limit.
- Avoid em dashes, sales language and unsupported cause-and-effect claims.
- Do not force a number when no useful, current public fact exists.

The seven Kuwait topic drafts now follow this rule. The six pending drafts have publicly visible
2032 endpoint evidence. Nothing pending is automatically published.

## Phase 2: crawl and graph evidence

Build on the existing inventory and graph services:

1. Add sitemap and sitemap-index discovery to `inventory.sources.public_web`.
2. Persist an immutable crawl-snapshot identifier on observed pages and edges.
3. Add read-only graph diff output: new/lost pages, new/lost edges, PageRank movement and depth
   changes.
4. Add a shortest-path query from an entry page to any target, including anchor text.
5. Add anchor-distribution and link-surface summaries by main content, navigation and footer.
6. Export the existing graph projection as CSV and GraphML.

Do not copy the external PageRank or HITS implementations because Ken already has tested,
versioned deterministic implementations and persisted metrics.

Acceptance measures:

- sitemap-only pages found;
- false orphan rate against a manual sample;
- graph audit runtime;
- percentage of pages deeper than three clicks;
- count of links to noindex, redirected or non-canonical destinations;
- reproducible diff between two snapshots.

## Phase 3: graph visualization

Extend the existing Next.js application and graph read API. Do not add Flask.

The screen should provide:

- cluster and page-family filters;
- node colour for indexed, noindex, orphan, priority and protected states;
- node size by PageRank or authority score;
- edge style for existing, proposed, approved and live links;
- click-to-open scorecard with source/destination evidence and anchor distribution;
- shortest path and before/after graph overlays;
- a small-subgraph default, with server-side filtering for the full corpus.

The D3 code should consume the existing API projection. PostgreSQL remains the source of truth.

## Phase 4: hybrid retrieval evaluation

Ken already has Qdrant semantic retrieval and a lexical channel. Add only the missing evaluation
and fusion work:

1. Record lexical rank, semantic rank, market/geography gates, graph features, editorial score and
   human decision for every candidate.
2. Add RRF as a shadow ranking option alongside the current weighted score.
3. Build a real Ken judgment set from dashboard approvals, revisions and rejections.
4. Measure precision@5, precision@10, recall@20, NDCG@10 and reviewer rejection rate.
5. Promote RRF only if it improves the agreed primary metric without weakening the hard gates.
6. Train a reranker only after sufficient real outcomes exist. Synthetic clicks must never be used
   to justify production promotion.

Qdrant remains the vector store under ADR-0017. Do not introduce pgvector or a second FastAPI
service.

## Delivery sequence and effort

| Slice | Estimated effort | Dependency | Deliverable |
|---|---:|---|---|
| Data-backed drafter and dashboard evidence | 1–2 days | Current v0.2 evidence | Completed for Kuwait drafts; generalize and test |
| Sitemap, snapshot diff and shortest path | 5–8 days | Inventory schema decision | CLI, API, fixtures and audit report |
| D3 graph review screen | 4–6 days | Graph read API and filtered endpoint | Interactive reviewer visualization |
| RRF shadow evaluation | 4–7 days | Real labeled proposal set | Side-by-side evaluation report |
| Real-feedback reranker | Later, 7–12 days | Enough approved/rejected and engagement outcomes | Shadow model, calibration and promotion decision |

## Expected overall result

After the first four slices, the realistic operational target is 25–45% higher human review
throughput and 40–60% less manual graph-audit time. Recommendation quality should be expressed as
measured precision and rejection-rate changes, not a guessed SEO percentage. Search indexing,
rankings and engagement remain outcome metrics measured through Search Console and first-party
events.

## Implementation status, 20 November 2026

The recommended order has now been implemented as a controlled MVP rather than as a second
engine:

1. Destination-data drafting is active for all seven Kuwait drafts. Each uses public destination
   evidence, ordinary punctuation and a maximum of one useful market endpoint.
2. `tools/graph-intelligence/graph-audit.mjs` builds the Kuwait snapshot, compares snapshots,
   answers shortest-path questions and discovers sitemap seeds.
3. The existing reviewer dashboard now shows a filtered interactive SVG graph. It reads the same
   proposal and audit files as the reviewer; no Flask app or duplicate graph database was added.
4. `services/ken_services/recommendations/rrf.py` implements deterministic shadow RRF, with five
   passing unit tests. Real dashboard decisions can be exported as a judgment set, and the
   readiness checker refuses reranker training until the minimum evidence threshold is met.

Current evidence is deliberately insufficient for a learned reranker: 6 decisions, 6 positive,
0 negative and 0 measured click outcomes versus required minima of 100, 40, 20 and 50. RRF and a
future reranker therefore remain shadow-only. The staging sitemap exposed no usable report URLs,
so sitemap discovery is implemented but needs a production or complete staging sitemap before a
coverage-gain claim can be measured.
