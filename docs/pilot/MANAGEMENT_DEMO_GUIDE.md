# Management demonstration guide: Agentic Interlinking OS (offline, synthetic)

Written for: Ken Research managers and the technology team deciding whether to authorise a real pilot.

**One sentence:** the system reads a site's pages, finds natural places to add internal links, explains and checks every
proposal, and records what it *would* publish; in this demo it ran on 75 fictional pages, published nothing, touched no
real website and measures nothing about SEO.

> Everything below is **synthetic**. The pages are fictional (`research.example.com`), the numbers are about how the
> software behaved, and none of them is a Ken Research outcome. Real content findings: **none yet**.

## 1. Five-minute walkthrough

1. Open `docs/pilot/offline_demo_report/INTERLINKING_REPORT.md` (the report). Banner first: it is synthetic.
2. **Section 1:** what was analysed: 64 base pages + 11 added pages (each labelled with where it came from), 0 real pages.
3. **Section 2-3:** the link architecture: 123 existing links, 12 of 72 published pages nobody links to; the extension adds
   orphans on purpose and the report shows which got proposals.
4. **Section 5:** all 56 proposals: source page, destination, suggested anchor, the surrounding sentence, a score
   broken into semantic / taxonomy / intent / graph / geography parts, the critic's verdict with its reason, and the
   shadow outcome. Open `recommendations.csv` for the full columns (exact character offsets, reason codes).
5. **Section 6:** why proposals were *not* approved: critic rejections (an anchor that misleads about the destination),
   uncertain answers (never treated as a yes) and confidence deferrals.
6. **Section 7 and the table before section 8:** where the engine found nothing, and where a designed situation did not
   produce the expected result (we show our misses).
7. **Section 9:** proof of safety: 0 deployments, 0 publish attempts, every decision recorded as `shadow`.

Live demo command (about 12 minutes, or 2 with the rule critic): `make offline-demo ARGS=--fresh` / `make offline-demo-fast`.

## 2. What the demo showed (all synthetic)

| Finding | Value |
|---|---|
| Pages analysed | 75 (64 + 11 synthetic), 72 published |
| Link proposals | 56 across 32 source pages |
| Local critic (qwen3:8b) | 40 accept, 14 reject, 2 uncertain, 0 fallbacks |
| Shadow outcome | 31 would auto-publish with extra verification, 23 would be blocked (14 critic rejects + 9 confidence deferrals), 2 declined |
| Hard safety blocks fired | 0 (protected, draft and ineligible pages were filtered out before proposals existed: 29 protected-target and 52 ineligible-target pairs) |
| Gold-judgment check (base corpus) | 80% of proposals on judged sources are GOOD or ACCEPTABLE; 0 proposals to a forbidden (BAD) destination; the critic accepted none of them |
| Misses we report | one designed spoke page produced no proposal; one designed orphan received none |
| Published | nothing |

The 80% is "agrees with hand-written fictional labels", not accuracy on real pages. 10 of the 50 judged proposals have no label.

## 3. Why it is safe to evaluate

* Shadow mode is the default and the runner forces it: decisions are recorded, nothing is written to a CMS.
* The critic can only say yes, no or unsure; **unsure and failed answers never become a yes**.
* Deterministic rules (protected pages, drafts, broken or unpublished targets, duplicate links, stale versions) are code,
  not model judgment, and run first.
* Every proposal is reproducible from stored facts and carries its explanation.
* Real content can only enter through a labelled, read-only import of files you already hold; nothing crawls.

## 4. Evidence still needed to establish real-world SEO effectiveness

None of this exists today. In order:

1. **Authorised real content.** An export or HTML folder of Ken Research pages (or the technology team's approval for the
   25-page crawl pilot). Without it nothing here says anything about Ken pages.
2. **Human relevance labels.** A few hundred proposals on real pages rated accept / reject / wrong anchor by editors:
   gives true precision, calibrates the critic and the 0.50 / 0.85 thresholds (currently uncalibrated by design).
3. **Template and placement validation.** Check that anchors land correctly in the real CMS templates (block hashes,
   existing link density, headings and widgets), using the simulated-CMS parity check on real HTML.
4. **Baseline search data.** Search Console and analytics for the pages involved (impressions, clicks, average position,
   crawl stats), before any change, for a defined period.
5. **A controlled rollout.** A small approved set of pages with a held-out control group (the system has a gated
   holdout mechanism), a pre-registered metric (for example clicks and impressions to destination pages, internal-link
   click-through) and a rollback path.
6. **Time and significance.** Weeks of data and a comparison to control; search effects are noisy and lag. Until then no
   claim of ranking or traffic improvement is supportable.
7. **Operational approvals.** CMS write access, an operator account and a decision to leave shadow mode; each is a
   separate approval and none is requested here.

## 5. What to say, and not to say

Say: "the pipeline works end to end offline, is explainable, safe by construction, and has a measured behaviour on a
fictional test set." Do not say: that it improves rankings, traffic or revenue; that the critic is accurate on Ken pages;
or that any synthetic number is a Ken Research result.
