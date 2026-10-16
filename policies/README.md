# Policies

Versioned **policy data**. TRD.md §12 requires ranking weights to live here, not in source code.

| File | Status | Loaded by |
|---|---|---|
| `ranking/ranking_weights.v1.yaml` | baseline (TRD §12) | `resolve_ranking_policy`, via the `ranking_policy:` pointer in the recommendation document — **not** from `Settings` |
| `recommendation/recommendation.v1.yaml` | baseline | `load_recommendation_policy` |
| `taxonomy/taxonomy.v1.yaml` | illustrative seed | `load_taxonomy` |
| `agents/agents.v1.yaml` | baseline | `load_configured_agent_policy` (cross-checks `max_plans_per_run <= recommendation.selection.max_plans_per_source`) |
| `analytics/analytics.v1.yaml` | baseline | `load_analytics_policy` |
| `learning/learning.v1.yaml` | baseline | `load_learning_policy` |
| `autonomy/autonomy.v1.yaml` | baseline (ADR-0018) | `load_autonomy_policy` — confidence floors, retry budget, risk thresholds; may only be MORE conservative than the hard floors in `ken_contracts.autonomy`; its version is recorded on every `autonomy_decisions` row |

All of them are served, validated and digested, by `GET /settings/policies` (`read_settings`,
`operator_admin` only).

## Rules

- **Never edit a published version in place.** Create `v2`. Every recommendation records the policy
  version that produced it (PRD §7.8), so past decisions must stay reproducible.
- **Lifecycle** (PRD §10): `PROPOSED → OFFLINE_TEST → SHADOW → CANARY → EXPERIMENT → PROMOTED`, or
  `REJECTED` / `ROLLED_BACK` / `WITHDRAWN`. From Phase 8 this is a contract
  (`ken_contracts.learning.ProposalStatus`) and a schema (`learning_proposals`); six stages are the
  MAXIMUM path and `policies/learning/learning.v1.yaml` declares the MINIMUM per kind. `CANARY` and
  `EXPERIMENT` are stage-defined with their machinery deferred to Phase 9.
- **What a proposal may change is a contract, not a convention.**
  `ken_contracts.learning.ALLOWED_CHANGE_PATHS` and `FROZEN_POLICY_PATHS` are `Final frozenset`s of
  dotted paths; `learning.v1.yaml` may only NARROW them. Nothing under
  `recommendation.placement`, nothing in `taxonomy` or `analytics`, no `Settings` value, no model
  name, no autonomy level and no part of the learning policy itself can ever be learned.
- **A promoted policy file is written only by the CLI** (`make policy-promote`), and only after a
  `promoted` transition with a `user:` actor and an `evaluation_run_id` already exists. For
  `ranking` it is TWO files atomically: the new weights file plus a new recommendation document
  whose `ranking_policy:` pointer names it.
- **No agent can promote a policy.** Agents may *propose* (PROPOSE_POLICY is ALLOW); promotion is
  PROMOTE_POLICY, an ASK-level human action (AUTONOMY_AND_PERMISSIONS.md).
- **AUTO_APPROVE stays disabled globally in v1** (TRD §20). It is enforced in code: the service refuses
  to start if `KEN_AUTO_APPROVE_ENABLED=true`.

`tests/unit/test_policy_data.py` validates every policy file's integrity.
