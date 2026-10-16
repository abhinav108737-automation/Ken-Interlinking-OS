# ADR-0018 — Autonomy-first publishing: no human approval in the normal linking workflow

- **Status:** Accepted, 2026-10-16. **Supersedes the human-approval workflow that ADR-0013 §1 made
  the normal path, and replaces the Phase 9 decision classes previously planned as
  BLOCK / REQUIRE_APPROVAL / AUTO_APPROVE_ELIGIBLE.**
- **Authority:** the user's final-completion directive of 2026-10-16: §1 ("There is no normal human
  approval step in the production linking workflow. Normal link publishing is policy-governed and
  autonomous"), §12, §13, §20 and §22 ("Do not design the system around an approval inbox").
- **Unchanged by this ADR:** every deterministic safety rule (§9 of the directive), CMS safety
  (§14–15), and the rule that **policy and skill promotion stays a governed, human control-plane
  action** (§18). An LLM still cannot self-promote anything, and still cannot override a
  deterministic filter.

## Context

Phase 5 (ADR-0013) built a reviewer control plane in which every plan waited in
`awaiting_approval` for a human to approve it, and Phase 6 would only deploy an `approved` plan.
The directive reverses that default: publishing is now governed by policy and autonomy decisions,
and humans exist for monitoring, policy administration, investigating blocks and rollbacks,
exception handling and operator intervention.

This is a change of **default**, not a removal of capability. The human controls are kept and
repurposed for exceptions, because the directive still requires operator override controls (§20)
and blocked-action investigation (§1).

## Decision

### 1. The autonomy decision

`AutonomyDecision` is one of:

| Decision | Meaning |
|---|---|
| `BLOCK` | never publish. Deterministic hard blocks and policy refusals land here. |
| `AUTO_PUBLISH` | publish without a human, then verify as usual. |
| `AUTO_PUBLISH_WITH_EXTRA_VERIFICATION` | publish, then run the strict verification profile (HTTP re-fetch plus a second pass after a delay, and a stricter mutation-isolation check). |

The critic's `NO_ACTION` is a separate, earlier outcome: the plan is not a defect, there is simply
nothing worth doing. It never becomes a publish.

**Hard blocks are deterministic and are evaluated before anything else.** A model cannot turn a
block into a publish; a static test asserts the block set is computed without any model call.

### 2. Autonomy modes

`KEN_AUTONOMY_MODE = shadow | guarded | autonomous`, **default `shadow`**.

| Mode | Behaviour |
|---|---|
| `shadow` | evaluate and record the decision that *would* have been taken. Publish nothing. |
| `guarded` | auto-publish only `AUTO_PUBLISH` decisions that are low-risk and high-confidence; everything else is deferred to an operator queue or blocked. |
| `autonomous` | publish every policy-eligible decision. Hard safety rules still apply. |

`autonomous` is never selected implicitly: a `Settings` validator refuses it unless
`KEN_AUTONOMY_AUTONOMOUS_ACKNOWLEDGED=true` is also set, and refuses it outright while the CMS
provider is `http` with writes enabled **unless** the operator has also set
`KEN_AUTONOMY_ALLOW_PRODUCTION=true`. The local and demo default stays `shadow`.

The legacy `KEN_AUTO_APPROVE_ENABLED` flag is **retired, not reused**. It meant "approve plans
without a human" in the old approval model and its startup validator still refuses `true`;
autonomy is now expressed only by `AUTONOMY_MODE`, so the two concepts cannot be confused.
`PoliciesView.auto_approve_enabled` keeps its `Literal[False]` pin.

### 3. Plan lifecycle

The operational states the UI and API speak in are: `proposed`, `blocked`, `auto_published`,
`verification_pending`, `verified`, `rolled_back`, `failed`.

Mapped onto the existing `LinkPlanStatus` ladder, this adds **`blocked`** and makes the
approval states exceptional rather than normal:

- normal: `proposed → critiqued → policy_checked → autonomy_decided → validated → deployed → verified → measured → learning_captured`
- blocked: `… → blocked` (terminal unless an operator overrides, which records who and why)
- exception: `awaiting_approval → approved` remains reachable **only** in `guarded` mode for a
  deferred decision, and for an operator acting on an exception. It is no longer on the normal path.

Transition authority is unchanged in kind: `system:` actors drive the autonomous path,
`user:` actors drive overrides, and agents still never insert, edit, approve, reject or expire a
plan (ADR-0012).

### 4. Verification and rollback become automatic

Every autonomous publish is verified (directive §15). On failure the system **rolls back by itself
when it is safe to do so** — an inverse splice on the current HTML, exactly as ADR-0014 defines —
records the failure, emits an audit event, and marks the pair so the same plan is not retried in a
loop. A retry budget per (source, target) and a cool-down make repeated unsafe retries impossible.
Rollback remains reversible, idempotent and version-aware.

`AUTO_PUBLISH_WITH_EXTRA_VERIFICATION` differs only in the verification profile, never in the
safety rules.

### 5. The human control plane

Humans keep: monitoring and dashboards; policy and skill administration and promotion;
investigating blocks and rollbacks; exception handling; manual intervention including a forced
rollback; and an operator override that can publish or block a specific plan with a recorded reason.
What they lose is the obligation to approve every link. The UI stops being an inbox: its primary
surfaces are the lifecycle states above, blocked actions, auto-published changes, verification
status and rollback history.

### 6. What is recorded

An `autonomy_decisions` row per plan evaluation: the decision, the mode in force, the risk tier,
the calibrated confidence, the critic outcome, the policy version, the deterministic blocks that
fired (as codes), whether rollback was available, and — in `shadow` — what would have happened.
This is what Phase 9 calibration and Phase 8 learning read, and it is append-only.

## Consequences

- The approval inbox stops being the centre of the product; `guarded` mode keeps a much smaller
  deferral queue for genuinely risky actions.
- Because the default is `shadow`, installing this product changes nothing on a real site until an
  operator deliberately moves to `guarded` or `autonomous` and acknowledges it.
- Phase 9's decision classes change name and meaning; the earlier `AUTO_APPROVE_ELIGIBLE` wording is
  retired in favour of the three decisions above.
- Behavioural analytics still may only *block* a graduation, never justify one (ADR-0016 §16.7),
  because first-party counts are spoofable.

## Alternatives considered

- **Keep approval as the default and add an autonomous mode behind a flag.** Rejected: the directive
  is explicit that autonomous, policy-governed publishing is the normal workflow.
- **Delete the approval machinery.** Rejected: §20 requires operator override and exception
  handling, and `guarded` mode needs somewhere to defer a risky action to. Deleting it would also
  throw away the editorial feedback stream that is the only trustworthy learning signal (ADR-0016 §2).
