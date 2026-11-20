# GitHub repository inheritance implementation

Date: 20 November 2026

## Outcome

The useful patterns from the three reviewed repositories are now represented inside the existing
Ken Interlinking OS. No external repository was copied wholesale, no second recommendation engine
was introduced, and all ranking changes remain shadow-only.

## Delivered

- Data-backed contextual drafts: every Kuwait topic proposal records public destination evidence,
  uses a reader-relevant fact where available, and avoids em dashes and sales wording.
- Controlled publishing: approved proposals `topic-kuwait:1` to `topic-kuwait:6` are CMS-verified
  and publicly rendered on the test host. Proposal 7 remains unpublished and unchanged.
- Graph intelligence: `tools/graph-intelligence/graph-audit.mjs` supports Kuwait graph snapshots,
  snapshot diffs, shortest paths and sitemap seed discovery.
- Reviewer visualization: the existing dashboard renders a filterable graph with existing,
  proposed and live edges, clickable report nodes and orphan highlighting.
- Hybrid-ranking foundation: deterministic reciprocal-rank fusion, precision@k and recall@k are
  implemented in `services/ken_services/recommendations/rrf.py`.
- Real-feedback controls: dashboard decisions are exported to
  `evals/datasets/kuwait-topic-real-judgments.json`; the readiness report prevents premature
  reranker training.

## Verification

- Approved topic batch: deployments 38–42, five of five CMS-verified and publicly rendered.
- Earlier approved canary: deployments 35–37, CMS-verified and publicly rendered.
- Graph snapshot: 28 nodes, 12 edges, including 6 live topic links and 1 unapproved proposal.
- RRF unit tests: 5 passed.
- Node syntax checks: dashboard, graph and ranking-evaluation scripts passed.
- Reranker readiness: blocked by design at 6 decisions, 0 negative labels and 0 measured clicks.

## Important limits

The test host declares `noindex,nofollow`; these deployments validate presentation and publishing,
not authority transfer. The sitemap endpoint returned no usable report URLs. RRF has not been
promoted into live ranking, and no click reranker has been trained. Those steps require real
candidate-channel ranks, enough negative reviewer decisions and measured engagement outcomes.

## Next evidence to collect

1. At least 100 reviewed proposals, including 20 genuine rejections.
2. At least 50 measured link-click outcomes with source, destination, placement and deployment ID.
3. A complete sitemap or production inventory snapshot for before/after graph coverage.
4. A real lexical-versus-semantic candidate export for the first RRF comparison using precision@5,
   precision@10, recall@20, NDCG@10 and reviewer rejection rate.
