# The local end-to-end demo

```bash
make db-up qdrant-up      # once: the project PostgreSQL (:5433) and Qdrant (:6333) containers
make demo                 # about 25 seconds (21-22 s measured); exit code 0 only if every check passes
make demo-clean           # drop the demo databases and delete the ken_demo_* Qdrant collections
```

`make demo` runs `infrastructure/scripts/demo.py`, a thin entry point for `services/ken_services/demo/`.
It prints a report to stdout (structured service logs go to stderr), ends with a summary table and exits
`0` (every acceptance check passed), `1` (a check failed or is missing) or `2` (it refused to run: a
dependency is down, or a resource is not the demo's own).

Optional, never required:

| Setting | Effect |
|---|---|
| `KEN_DEMO_ADMIN_PASSWORD=... make demo` | Also creates the login `demo_admin` (role `operator_admin`) on the **demo** database with exactly that password, so a human can browse the UI. Without it no human account exists. |
| `make demo ARGS=--ollama` | Real embeddings from the local Ollama daemon instead of the deterministic mock (the critic stays scripted). The report is then not byte-reproducible. Not exercised in the verification of this document. |

To browse the result after a run (the databases are left in place until `make demo-clean`):
`KEN_DATABASE_URL=postgresql+asyncpg://ken:ken@localhost:5433/ken_demo make dev-api`, with
`KEN_DEMO_ADMIN_PASSWORD` set when the demo ran.

## What is real and what is not

Everything in the report is **synthetic**: a fictional 64-page research site
(`tests/fixtures/demo/research_site.jsonl`, reserved host `research.example.com`), a **simulated CMS** in
its own database, a **deterministic mock model**, no real crawl, no Google service and no network beyond
`127.0.0.1`. The services under test are the production ones: the ingestion, analysis, indexing, graph,
recommendation, agent, autonomy, deployment, collector, analytics and learning code that the CLIs call.
The demo re-implements none of it; `services/ken_services/demo/` only sequences library calls, reads
the database back and asserts.

## Isolation (the demo refuses to run anywhere else)

| Resource | The only names the demo accepts |
|---|---|
| Application database | `ken_demo` (the integration test uses `ken_demo_test`) on `127.0.0.1:5433` |
| Simulated CMS database | `ken_cms_sim_demo` (`ken_cms_sim_demo_test`) on `127.0.0.1:5433` |
| Qdrant collection | `ken_demo_pages` (`ken_demo_test_pages`) on `127.0.0.1:6333`; `demo-clean` deletes every `ken_demo_*` |
| Servers | the simulated site and the collector bind `127.0.0.1` port 0 and verify their own listener (never 8765) |

The two databases are dropped and recreated at the start of every run. `DemoConfig` validates every
name against a closed pattern before any connection is opened; the developer's PostgreSQL (5432), the
dev database, `ken_cms_sim`, every other `ken_*` database and other projects' services are unreachable
by construction. Settings are built from explicit values with `_env_file=None`, so a `.env` can never
point the demo at the dev database.

The publisher needs an operator identity (`deployments.actor` carries a `^user:` CHECK).
`demo-autonomy-service` is created on the demo database with a random password that is generated at
run time, hashed, never printed and never stored, so nobody can log in as it.

## Stages

| # | Stage | Entry point it calls | Printed from the database |
|---|---|---|---|
| 1 | Disposable resources, servers, identities | `resources`, `SimulatedSite`, `CollectorServer` | listener URLs, identities |
| 2 | Ingest, normalise, version; re-ingest is a no-op | `run_ingestion` | pages, versions, links, states |
| 3 | Seed the simulated CMS | `seed_simulated_cms` | CMS pages, HTTP 200 + beacon tag |
| 4 | Analyse and embed | `run_analysis` | page types, embeddings, protections, relationships |
| 5 | Qdrant index, verify, rebuild, verify | `run_index_job`, `PostgresVectorVerifier`, `run_rebuild_job` | drift report twice, 5-NN probe identical |
| 6 | Graph metrics | `run_graph_metrics` | orphans, entry page, bridges, clusters |
| 7 | Candidates (4 generators), filters, ranking, anchors, placement | `run_recommendations` | candidates by channel and rejection code, one score breakdown, one before/after |
| 8 | The critic | `run_agent_batch` | verdicts, hand-offs |
| 9 | Shadow and guarded modes | `run_autonomy`, `AutonomousPublisher` | counterfactuals, deferrals |
| 10 | BLOCK | `AutonomousPublisher` | the decision row, the CMS write ledger |
| 11 | NO_ACTION | `DatabaseAutonomyEngine` | zero decision rows |
| 12 | Failed verification, rollback, retry refused | `AutonomousPublisher` | attempts, events, bytes |
| 13 | Autonomous pass (strict verification) | `run_autonomy` | decision, attempt, deployment, verification passes, live page |
| 14 | Calibration gate and plain AUTO_PUBLISH | engine + SYNTHETIC fixture | decision before and after |
| 15 | Analytics through the real collector | `run_aggregation` | events, daily rows, measurements |
| 16 | Learning | `evals/runner.py`, `run_reflection`, `run_evaluation` | suites, gate, proposal |
| 17 | Governance | `DatabasePromotionService`, raw SQL | typed refusals, SQLSTATE 55000 |

## The nine acceptance checks

Each is asserted in code; a missing or failed one makes the exit code non-zero. Numbers are from a real
run (they are stable: see Repeatability).

1. **BLOCK** (stage 10). A plan the critic *accepted* (stored review `accept`, `handed_off`) has its
   destination protected by an editor. In `shadow`, `guarded` and `autonomous` the engine decides `BLOCK`
   with codes `protected_page` and `policy_disallowed_destination`; the autonomous publisher returns
   `blocked`. The CMS write ledger stays at 0 calls, deployments at 0, and the source page is unchanged.
   Proves: a model verdict cannot clear a deterministic code.
2. **AUTO_PUBLISH_WITH_EXTRA_VERIFICATION** (stage 13). 26 uncalibrated plans publish in acknowledged
   `autonomous` mode under the strict profile. For one of them the demo prints the decision row, the
   publish attempt (`strict`, succeeded), the deployment (`verified`), two verification passes (each with
   an HTTP check) and that `data-ken-link-id` is in the CMS page and in the page the site serves. No plan
   passed through `awaiting_approval` or `approved`.
3. **NO_ACTION** (stage 11). Three plans were reviewed `uncertain` (not handed on). The engine raises
   `CriticDeclinedError`, the publisher returns `declined`, the plans stay `proposed`, and there are 0
   decision rows, 0 deployments, 0 attempts and no CMS call.
4. **AUTO_PUBLISH, plain** (stage 14). For one plan of the class `parent_market|target_topic|graph+lexical+semantic+taxonomy`
   the engine decides extra verification (no calibration, `strict`). A **SYNTHETIC calibration fixture**
   is then written (see below) and the same plan decides plain `AUTO_PUBLISH` under the `standard`
   profile with a `calibration_version`; the 7 plans of the class publish that way.
5. **Rollback** (stage 12). Verification is made to fail by injecting a faulted verifier (below). The
   publisher rolls back by itself: the plan is `rolled_back`; the CMS page is byte-identical to its
   original, in the CMS and over HTTP; `link.failed` and `link.rolled_back` are emitted; the attempt is
   recorded as failed with a cool-down; when the recommender re-proposes the pair, publishing it is
   `deferred` with `cooling_down`, with no new attempt and no CMS call.
6. **Modes** (stages 9, 13). `shadow` records 44 counterfactual rows (39 would publish with extra
   verification, 5 would block) and changes nothing. `guarded` on three of the same plans publishes
   nothing: each ends `blocked` with an empty block list and the readable reason "calibration absent ...
   risk tier medium (guarded needs low) ... below the guarded floor 0.85". `autonomous` publishes. A
   deferred plan stays `blocked` until an operator re-opens it; that is why guarded ran on a sample.
7. **Analytics** (stage 15). 8 page views (16 events) are POSTed over HTTP to the real collector from the
   site's origin and stored. A foreign-origin request and a bot-User-Agent request are answered `204`
   (indistinguishable from success) and store nothing; the rejection counters show `origin_not_allowed`
   and `bot`. Aggregation writes 8 daily rows, all `synthetic`; the sweep moves 0 plans to `measured`,
   because the only coverage a measurement may use is non-synthetic and there is none.
8. **Learning** (stage 16). `evals/runner.py --suites all` runs 12 suites (all pass) and the gate is
   `inconclusive`: three decision classes have fewer than the 30-sample floor. Reflection on the real
   demo history writes no proposal (no human reviewed anything). Reflection on the SYNTHETIC review
   history creates one proposal (`ranking`, status `proposed`, `proposed_by system:learning`); the
   offline evaluation bound to it is `inconclusive`. Promotion is attempted and refused with the typed
   `demonstration_mode` error. Two changes to frozen paths are refused with `frozen_field`.
9. **Governance** (stage 17). Three raw SQL promotions (an `UPDATE` of a proposal, an `INSERT` of a
   `promoted` transition without the promoter setting, an `UPDATE` of a skill) are each refused by
   PostgreSQL (SQLSTATE 55000). No proposal or skill is promoted and no file under `policies/` changes.

## Synthetic and injected things, all labelled

* **Calibration fixture** (stage 14, banner "SYNTHETIC CALIBRATION FIXTURE - not evidence about any real
  site"). One passing, fresh `policy_evaluations` row, `runner_version = 'demo-synthetic-calibration'`,
  40 samples, calibrated confidence 0.95, positive-outcome rate 0.60, for exactly one decision class, bound
  to a system-authored proposal titled "SYNTHETIC CALIBRATION FIXTURE" (the schema needs exactly one
  subject). Written through the tables the learning pipeline writes, around no guard. It is what makes
  plain `AUTO_PUBLISH` reachable; a fresh install has nothing like it.
* **Review history** (stage 16, banner "SYNTHETIC REVIEW HISTORY"). 34 plans of one class dated June 2026,
  17 approved and 17 rejected, by a `demo-synthetic-reviewer` account with no known password.
* **Fault injection** (stage 12). The link verifier is replaced by one that reports every check failed,
  leaving the CMS page untouched (the same instrument as `tests/integration/autonomy_support.py`). Any
  fault that also moves the CMS page makes the CMS correctly refuse the rollback.
* **Scripted critic.** The mock model returns `accept` for every plan except those of one source, which it
  returns `uncertain` for. It says nothing about a real critic.
* **Mock-embedding floor.** The semantic floor is 0.30 (the shipped 0.78 is for a real embedding model);
  every later gate is unchanged.
* **Stand-ins for people.** The editor who protects a page and the browser that sends beacons are the demo.

## What this does NOT prove

* **Real-template HTML parity.** The corpus is simplified HTML; real templates, widgets and markup quirks
  are untested.
* **Real CMS behaviour.** The CMS is a simulator we wrote: publish lag, authentication, caching,
  concurrent editors and API errors of a real CMS are not exercised.
* **A live model's quality.** Critic verdicts and embeddings are deterministic mocks; the precision of
  real verdicts and of real semantic retrieval is not shown.
* **Real traffic.** No browser ran `k.js`; the demo POSTs what the beacon would send. Nothing was measured
  on real users, and no plan reached `measured`.
* **Real review history.** Calibration and review data are synthetic. Whether calibration, learning or a
  promotion would be sound on real data is not shown. Guarded mode publishing a *calibrated* class is
  not shown either: the shipped floor (0.85) is above anything this corpus scores.
* **Production gates.** `KEN_AUTONOMY_ALLOW_PRODUCTION` and an `http` CMS provider are never involved.

## Repeatability

Two runs produce the same decisions and the same report body once generated ids, timestamps, ports and
the elapsed time are replaced (`ken_services.demo.console.normalise`). Verified by running the demo
repeatedly, normalising and diffing: byte-identical, also across `PYTHONHASHSEED` values. A first
comparison found one real difference, in the lexical candidate generator: ties on `ts_rank_cd` were
broken by `pages.id`, a random UUID per install, so the k-th candidate (a candidate that never became a
plan) differed between installs of the same corpus. The tie-break is now canonical URL first
(`recommendations/generators.py`). Not removed: nothing known.

## Files

`services/ken_services/demo/` (`config`, `console`, `context`, `resources`, `pipeline`, `publishing`,
`measurement`, `learning`, `synthetic`, `faults`, `runner`, `__main__`), `infrastructure/scripts/demo.py`,
`Makefile` (`demo`, `demo-clean`), `tests/integration/test_demo_end_to_end.py`.
The test runs the demo with the mock provider against `ken_demo_test`, `ken_cms_sim_demo_test` and
`ken_demo_test_pages` and asserts every check and an independent read-back:
`KEN_TEST_DATABASE_URL=postgresql+asyncpg://ken:ken@localhost:5433/ken_demo_test .venv/bin/pytest tests/integration/test_demo_end_to_end.py`.
