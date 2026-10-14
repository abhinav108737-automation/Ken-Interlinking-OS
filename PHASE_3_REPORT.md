# Phase 3 Report — Recommendation Engine

**Status: complete.** Executed under the user's 2026-10-14 authorisation for autonomous sequential
delivery of Phases 2–9. Each phase has internal gates, a local commit and a `phase-N-complete` tag.
Nothing has been pushed.

**Design:** [docs/adr/ADR-0011-recommendation-engine.md](docs/adr/ADR-0011-recommendation-engine.md).
**Evals:** [evals/recommendations/README.md](evals/recommendations/README.md).

## 1. What was built

A **deterministic** engine turns the Phase 2 understanding into reviewable, explainable link
proposals (`LinkPlan`s). No LLM is involved, and nothing is fabricated: every anchor is copied
verbatim from the source page.

| Capability | Implementation |
|---|---|
| **Retrieval** | Four switchable channels: `semantic` (pgvector cosine, identity-filtered), `lexical` (PostgreSQL full text over a stored `tsvector`, bound `plainto_tsquery`), `taxonomy` (stored relationships), `graph` (under-linked targets in the industry). Union capped by the sum of per-channel-normalised scores. |
| **Filters** | 17 ordered `CandidateRejection` codes, first rejection recorded: self link, protected source/target, experiment lock (hook for Phase 7), ineligible source/target (live), already linked (through `known_urls`), duplicate (open or live plan), reviewer-rejected cool-down, excluded target path (home page), page type / intent, relevance, no natural anchor, final score, link density, per-source and per-target caps. |
| **Score** | Transparent `ScoreBreakdown`: relevance = Σ weighted components (`ranking_weights.v1`), total = relevance × placement quality; semantic floor recorded. Raw confidence only (`calibration_version = null`). |
| **Placement** | Anchor phrases from the target's market, confident sub-market topics, title core and keyword bigrams. Whole-phrase, one text run, no excluded ancestor (links, tables, quotes, code, headings, labels, media fallback, header/footer …), no overlap, never a fragment of a longer market phrase, never a geography. **No self-describing anchors** (§9.1 of the ADR), with a commercial-hop exception. |
| **Plans** | `link_plans` with immutable content (generic trigger), append-only `link_plan_transitions`, one open plan per pair and per anchor span, pinned source and target versions. One transition service: `ALLOWED_TRANSITIONS` + **actor authority** (`TRANSITION_AUTHORITY`: only `user:` approves/rejects; only `system:` deploys/expires; agents never decide). |
| **Freshness** | `check_plan_freshness`: fresh / rebasable / stale with reasons and the matched `block_id`. Stale plans expire when the source changes; **invalid** plans (page now protected, gone, ineligible, or already linked) expire on every run (fail closed). |
| **Audit** | Every retrieved candidate is stored in `link_candidates` (channels, raw scores, total or rejection). `link.proposed` per plan, `job.completed` per run. |
| **Opportunities** | orphan, under-linked and link-capacity signals per page, replaced per run. |
| **API** (GET-only) | `/recommendations` (filters, paging), `/recommendations/{id}`, `/summary`, `/candidates`, `/opportunities`. |
| **UI** | Recommendation inbox, plan detail (verbatim placement preview, score breakdown, rationale, history), candidates audit, opportunities, and a Recommendations panel on page detail. No mutation controls (Phase 5). |
| **CLI** | `make recommend` (idempotent); `make analyze` precedes it. |

## 2. Schema — migration `0004_recommendations`

`link_plans`, `link_plan_transitions`, `link_candidates`, `opportunities`, plus
`page_versions.search_vector` (stored generated `tsvector`, bounded input, GIN index).

- Content guard and append-only triggers.
- Partial unique indexes: open pair, open span, single successor.
- CHECKs on statuses, rejection codes, offsets, score ranges and hash format.
- RESTRICT foreign keys.

Round trips verified: 0004 → 0003 → head, and base → head. **0004 was edited in place before
release** (the `target_excluded` code). The local test, QA and dev DBs were rebuilt. It is
immutable from this tag on (ADR-0011 §9.8).

## 3. Results on the synthetic demo corpus (65 pages, 46 judged sources)

| Provider | Plans | P@3 | Recall GOOD | BAD rate | Anchor exact | Must-not violations | Determinism after re-ingest |
|---|---|---|---|---|---|---|---|
| mock | 48 | 0.98 | 0.78 | 0.021 | 1.00 (11/24) | 0 of 20 rules | identical |
| local `nomic-embed-text` | 48 | 1.00 | 0.80 | 0.000 | 1.00 (13/24) | 0 | identical |

- **Dev DB:** `make recommend` proposed 43 plans. A rerun proposed 0, with all 43 pairs rejected
  as `duplicate_recommendation`. After the QA re-review fixes, a further run added 5 parent-market
  plans (48 in total) and expired or invalidated nothing.
- **Ablations:** on 65 pages, channel ablations barely move the numbers. Placement is the binding
  constraint.
- **Recall is over 30 GOOD pairs, after rule R5** (§6, M3).

## 4. Dependencies

**None added.** Full-text search is built into PostgreSQL. No new infrastructure service.

## 5. Verification (final run, on the final code)

| Gate | Result |
|---|---|
| `make verify` | **exit 0**: ruff clean, mypy clean (204 files), **2,260** Python tests passed, **709** web tests passed, compose ×3 OK, migrations at head, doctor **0 FAIL / 0 WARN** (21 checks) |
| Live Ollama tests (`make test-live`) | 5 passed |
| Recommendation evals | 20 must-not rules at zero tolerance; floors P@3 ≥ 0.90, recall ≥ 0.70, anchor ≥ 0.85, BAD ≤ 0.03: all pass |
| OpenAPI drift | regenerated; snapshot test green; `types.gen.ts` current |
| Electron guard | web lint clean; no Electron import; text nodes only (no raw HTML rendering) |
| Live API smoke test (dev DB) | summary, list, candidates, opportunities and detail return 200; unknown id 404; malformed id 422; POST 405 |

## 6. Reviews

**Pre-fan-out architecture review.** Its required changes were applied before any specialist
work: contract adjustments, the generic content trigger, pinned target version and
freshness semantics (commit `52b1424`).

**Eval-driven fixes during integration** (ADR-0011 §9):
- determinism: canonical-URL source order and URL tie-breaks;
- the self-describing-anchor rule;
- secondary topics limited to sub-markets of the target's market;
- home page excluded as a target;
- open-plan diversity seeded from the DB, which fixed run-to-run idempotency;
- scale-free candidate cap;
- freshness `block_id` and the edited-block diagnostics.

**Final architecture review.** It added the commercial-hop exception (article → report on the
same market was being blocked), the `target_excluded` code, the rename to
`max_open_plans_per_target`, and documentation fixes.

**QA/security review: blockers found, all fixed.**

| Finding | Fix |
|---|---|
| **H1** Fragment anchors ("battery energy" placed inside "battery energy storage"; a keyword bypassed the own-market rule) | Word-sub-span own-market check; strict-fragment guard against all market phrases and the target's phrases; new must-not `MN-fragment-anchor`; unit tests |
| **H2** Open plans survived a page becoming protected or gone | `_expire_invalid` expires (`invalid: …`) open plans whose source or target is ineligible or whose pair is now linked; counter `plans_invalidated`; integration test |
| **H3** No authority check on transitions (an agent could approve and deploy) | `TRANSITION_AUTHORITY` in the contract, `UnauthorizedActorError`, a test over every allowed edge and actor class. Authentication of `user:` is a **Phase 5 hard gate** |
| M1 Placement exclusions incomplete | `excluded_ancestors` extended (labels, struck/small text, media fallback, header/footer/dialog/details …); inline `display:none` is a Phase 6 item |
| M2 Status UPDATE possible without history at DB level | **Phase 6 hard gate** (SECURITY DEFINER / least-privilege role) |
| M3 Eval labels contradicted the anchor rule; recall floor 0.15 was not meaningful | Deterministic relabel rule R5: 45 GOOD → ACCEPTABLE, tagged, still positives. Recall floor **restored to 0.70** (measured 0.78/0.80) |
| M4 In-place edits of 0004 and policy v1 | DBs rebuilt; pre-release amendment and immutability documented (ADR-0011 §9.8) |
| M5 Scale (exact KNN, correlated subquery) | Known limitation, ANN plan recorded |
| Lows | target title capped at 300 chars in plans; per-source comment; per-target slot freed after expiry; URL tie-break |

**QA re-verification: PASS WITH REQUIRED FIXES, all applied.** All eight items above were
verified with probes, including a mechanical re-derivation of rule R5 (45 predicted, 45
relabelled). The H2 fix itself introduced issues, fixed before the commit:

| Finding | Fix |
|---|---|
| **High.** Expiry fired on analysis lag: after a re-ingest, `recommend` without `analyze` expired 10 plans, including approved ones | Only DURABLE ineligibility expires a plan (`DURABLE_INELIGIBILITY`). Lag and HTTP errors pause generation. A regression test was added and mutation-checked: it fails if `stale_analysis` counts as durable |
| Medium. The fragment rule also dropped parent-market topic anchors ("logistics" on a cold-chain page) | Sub-span check limited to title cores and keyword bigrams; topics match exactly; test |
| Medium. The commercial-hop exception was untested and counted any non-commercial intent | Hop requires an INFORMATIONAL source; unit tests for article, report, navigational and transactional sources, and for the distinctive-geography case |
| Low. No automated actor can reach terminal `rejected` | Phase 4 design: agent-dropped candidates are never persisted (ADR-0012) |

## 7. Incidents (disclosed)

- **The QA reviewer's first test run targeted `ken_interlink_test`,** the orchestrator's test DB,
  because `KEN_TEST_DATABASE_URL` was unset. It holds disposable fixture data only, and the tests
  rebuilt it. Later runs used `ken_interlink_qa_test`. Reviewer prompts now always set the
  variable.
- **The working tree changed during the QA review** because integration was still in progress.
  The QA verdict covered HEAD plus that work. All gates were re-run on the final tree.

## 8. Known gaps

See NEXT_STEPS.md ("Hard gates carried into later phases" and "Phase 3 known limitations"):
- authenticated actors (Phase 5);
- deploy-time re-validation and DB-level status integrity (Phase 6);
- `display:none` detection (Phase 6);
- ANN index at scale;
- raw confidence until Phase 8;
- the stored-column migration lock on a large production table.

## 9. Information still needed from you

- **Editorial decision (new):** should same-market, cross-country pages link on the plain market
  name? The orchestrator default is no, unless the block names the target's country or region,
  or the link is an article → report hop. One policy switch flips it.
- Everything listed in NEXT_STEPS.md "Needed from you before real production ingestion" (source,
  scope, taxonomy, protections) still applies. Real-site quality depends on Ken's taxonomy.

## 10. Commit

`feat: complete Phase 3 Recommendation Engine`, tag `phase-3-complete` (local only).
