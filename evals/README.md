# Evaluations — Ken Interlink OS

> **Phase 0 status: STRUCTURE ONLY. No suites implemented.**
> There are no eval datasets, runners, scorers or metrics yet. This file
> and the suite READMEs reserve the layout and record the rules that later
> phases must follow. Anything marked **Proposed** is not frozen and is not in
> the governing specs. It must be confirmed (ADR or user decision) before
> anyone implements it.

## 1. Purpose

Evals measure the **quality of probabilistic decisions**: which destination,
which anchor, which relationship, whether the critic was right, whether
"do nothing" was correct. Tests measure the **correctness of deterministic
code**. See [`tests/README.md`](../tests/README.md) for that split.

Evals also act as the **gate on activation**. No skill, policy or autonomy
change reaches production without passing them first (PRD §9–§11, TRD §17–§20).

## 2. The hard rule: evaluation gates activation, and proposal is separate from promotion

| Action | Permission (AUTONOMY_AND_PERMISSIONS.md) |
|---|---|
| `RUN_OFFLINE_EVAL` | ALLOW |
| `PROPOSE_SKILL` / `PROPOSE_POLICY` | ALLOW |
| `PROMOTE_SKILL` / `PROMOTE_POLICY` | **ASK** (explicit human approval) |
| `CHANGE_AUTONOMY_LEVEL` | **ASK** |
| Silent policy self-promotion | **BLOCK** |

- Agents may **propose**. They can **never promote**. The Learning agent
  "produces skill/policy proposals only" (TRD §10).
- A passing eval is **necessary but not sufficient**. Promotion is a separate,
  explicit, audited human action.
- `AUTO_APPROVE` is disabled globally in v1 (TRD §20). Evals do not change
  that.

### Controlled learning pipeline (allowed)

```
experience
  → structured feedback
  → proposal (hypothesis)
  → offline eval          ← this directory
  → shadow
  → canary
  → experiment
  → explicit promotion    ← human, ASK-level
```

Policy lifecycle (PRD §10):
`PROPOSED → OFFLINE_TEST → SHADOW → CANARY → EXPERIMENT → PROMOTED`, or
`REJECTED` / `ROLLED_BACK`.

### Forbidden

```
experience → immediate production self-modification
```

(TRD §18, CLAUDE.md "Learning Design Rule".)

## 3. Suites

The first seven come from TRD §19 (minimum suites). `policy-regression` comes
from the eval-learning-engineer role definition and supports PRD §10.

| Suite | Measures | Node(s) exercised (TRD §9–§10) | First populated |
|---|---|---|---|
| [destination-selection](suites/destination-selection/README.md) | Are the chosen destinations relevant, eligible and well ranked? | Discovery: `RETRIEVE` → `FILTER` → `RANK` | Phase 3 (deterministic baseline); Phase 4 (agent fixtures) |
| [anchor-selection](suites/anchor-selection/README.md) | Is the anchor existing, natural source text, at a stable placement? | Planner: `PLACE` | Phase 3 (placement); Phase 4 (planner node) |
| [relationship-detection](suites/relationship-detection/README.md) | Are classification and PRD §7.3 relationship types correct? | Understanding: `UNDERSTAND` | Phase 2 |
| [critic](suites/critic/README.md) | Does the critic accept good plans and catch bad ones? | Critic: `CRITIQUE` | Phase 4 |
| [no-action](suites/no-action/README.md) | Does the system correctly decide to do nothing? | Triage: `TRIAGE` (`NO_ACTION`); Planner | Phase 4 |
| [protected-content](suites/protected-content/README.md) | Is protected content never proposed, approved or mutated? | `FILTER`, `POLICY_GATE`, `VALIDATE` | Phase 3 (eligibility); Phase 6 (CMS adapter) |
| [stale-page-version](suites/stale-page-version/README.md) | Does a version conflict force regeneration and never publish? | `VALIDATE` → `PUBLISH` (409 path) | Phase 6 |
| [policy-regression](suites/policy-regression/README.md) | Does a candidate policy regress against history and safety cases? | `POLICY_GATE`, ranking policy | Phase 8 (policy proposals); Phase 9 (autonomy policy engine) |

Phase 1 also has a deterministic inventory fixture corpus (no model, pass/fail goldens):
[inventory/README.md](inventory/README.md).

Phase 2 adds a synthetic demo corpus with analysis/eligibility goldens, labelled relatedness
pairs and a retrieval/embedding benchmark (mock and local Ollama):
[intelligence/README.md](intelligence/README.md).

Phase 3 adds recommendation judgments, must-not gates, freshness fixtures and a channel/component
ablation benchmark (mock and local Ollama): [recommendations/README.md](recommendations/README.md).

Phase 8 ("Evaluation datasets … offline evals") is when these suites become
formal, versioned datasets with a runner. Before Phase 8, the phases above may
add recorded fixtures as they build the corresponding nodes.

## 4. Fixture format (Proposed — NOT frozen, NOT implemented)

One case per file, or JSON Lines per suite. The choice is deferred to Phase 8.
`pyyaml` is already an approved dependency, so YAML is one option. Skeleton
only, with placeholder values:

```yaml
case_id: "<suite>-<stable-slug>"        # stable; never reused
suite: "<suite name>"
fixture_version: 1                       # bump on any change to expected/inputs
decision_class: "<class label>"          # for TRD §20 per-class metrics
description: "<one sentence: what this case checks>"

inputs:                                   # everything needed to replay offline
  source_page: "<path to a recorded/synthetic page snapshot under the suite>"
  source_content_version: "<opaque version string>"
  candidates: "<recorded candidate set, if the node consumes one>"
  policy_version: "<policy id/version under test>"
  recorded_model_responses:               # replayed by MockModelProvider
    - role: "fast | reasoning | embedding"  # by ROLE, never by model name
      request_fingerprint: "<hash of the normalized request>"
      response: "<recorded structured output or raw text>"

expected:
  decision: "<the correct decision, e.g. destination id / NO_ACTION / BLOCK>"
  acceptable_alternatives: []             # other decisions that also pass
  must_not: []                            # decisions that are a hard fail

rationale: "<why the expected decision is correct; human-written>"

provenance:
  origin: "synthetic | anonymized_from_review | regression_from_incident"
  created_by: "<human or agent id>"
  created_at: "<ISO-8601 date>"
  source_refs: []                         # e.g. review id, incident id, ADR

privacy:
  contains_production_data: false
  approval_ref: null                      # required if the line above is true
```

### Privacy and safety rules for fixtures

- **No real customer or production data without explicit user approval.**
  The default is synthetic content. Approved production-derived cases record
  `approval_ref`.
- **Fixtures must never contain secrets.** That includes CMS credentials,
  API keys, connection strings, session tokens and analytics tokens
  (CLAUDE.md constraint 20).
- External identifiers (`PageId`, `LinkId`, `DeploymentId`) are opaque. Store
  them verbatim and never derive meaning from them (docs/CONTRACTS.md rule 6).
- A `ModelSchemaViolationError` carries `raw_text` so that a malformed model
  output can become a fixture rather than being discarded
  (`ken_contracts/model.py`). Scrub that text with the same privacy rules
  before committing it.

## 5. Metrics (to be tracked later — none implemented in Phase 0)

Per decision class, from TRD §20:

| Metric | Meaning |
|---|---|
| `sample_count` | Decisions observed in this class |
| `approval_rate` | Share approved without edits |
| `edit_rate` | Share approved after anchor/destination/placement edits |
| `rollback_rate` | Share of deployments later rolled back |
| `positive_outcome_rate` | Share with non-negative measured outcome (Phase 7 analytics) |
| calibrated confidence | See below |
| `autonomy_level` | Current BLOCK / REQUIRE_APPROVAL / AUTO_APPROVE setting |

Per suite: pass rate, plus **baseline score vs candidate score** for every
skill or policy candidate (role definition: "Skill evaluation").

Model-quality signals already present in the frozen contracts:
`StructuredResult.attempts` and `StructuredResult.repaired`
(`ken_contracts/model.py`).

### Calibration

Track **predicted confidence against observed approval/correctness**.
**Autonomy decisions must use calibrated confidence, never raw model
confidence** (role definition; PRD §7.8 stores "calibrated confidence" on every
LinkPlan). Raw LLM self-reported confidence is an input to calibration, not
the output.

Proposed: reliability bins and expected calibration error per decision class.
The method is to be confirmed in Phase 9.

Graduation from ASK to ALLOW also requires a minimum sample size, acceptable
edit and rollback rates, non-negative outcomes **and an explicit policy that
authorizes it** (AUTONOMY_AND_PERMISSIONS.md, "Progressive Autonomy").

## 6. How evals will run

- **Offline by default.** `RUN_OFFLINE_EVAL` is ALLOW-level and touches no
  production system and no CMS.
- **Deterministic by default.** Evals replay recorded model responses through
  `MockModelProvider` (`KEN_MODEL_PROVIDER=mock`). The same fixture always
  produces the same score, so CI can gate on it.
- **Live-model runs are opt-in.** Running against local Ollama measures a new
  model or prompt. It is explicitly requested and never part of the default CI
  path. The env-var mechanism should match the `live` test marker
  (`KEN_RUN_LIVE_OLLAMA=1`). Proposed: reuse it.
- **Local only, no paid APIs.** No OpenAI, Anthropic, Gemini or other paid
  inference, and no hosted eval or LLM-judge services (CLAUDE.md constraints
  1–3). Any judge model is a local `ModelProvider` role.
- **Models by role.** Fixtures and runners reference `fast` / `reasoning` /
  `embedding`, never a model name.
- **Read-only results.** An eval run writes a report. It never activates,
  promotes or reconfigures anything.
- Runner and CLI: none exist yet. Proposed for Phase 8.

## 7. Layout

```
evals/
  README.md                 ← this file
  suites/
    <suite>/README.md       ← scope, example cases, failure modes, phase
    <suite>/cases/          ← Proposed (Phase 2–8): fixture files
```
