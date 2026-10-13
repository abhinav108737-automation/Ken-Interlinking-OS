# Policies

Versioned **policy data**. TRD.md §12 requires ranking weights to live here, not in source code.

| File | Status | Loaded by |
|---|---|---|
| `ranking/ranking_weights.v1.yaml` | baseline (TRD §12) | nothing yet — the Phase 3 ranking service |

## Rules

- **Never edit a published version in place.** Create `v2`. Every recommendation records the policy
  version that produced it (PRD §7.8), so past decisions must stay reproducible.
- **Lifecycle** (PRD §10): `PROPOSED → OFFLINE_TEST → SHADOW → CANARY → EXPERIMENT → PROMOTED`, or
  `REJECTED` / `ROLLED_BACK`.
- **No agent can promote a policy.** Agents may *propose* (PROPOSE_POLICY is ALLOW); promotion is
  PROMOTE_POLICY, an ASK-level human action (AUTONOMY_AND_PERMISSIONS.md).
- **AUTO_APPROVE stays disabled globally in v1** (TRD §20). It is enforced in code: the service refuses
  to start if `KEN_AUTO_APPROVE_ENABLED=true`.

`tests/unit/test_policy_data.py` validates every policy file's integrity.
