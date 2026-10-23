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

## Amendments settled during the contract freeze (2026-10-16)

1. **The production gate covers `guarded` as well as `autonomous`.** §2 gated only `autonomous` behind
   `KEN_AUTONOMY_ALLOW_PRODUCTION`. `guarded` also publishes without a human, so a gate covering only
   the louder mode is not a gate. Verified: against the simulated CMS (the local demo path) `shadow`,
   `guarded` and acknowledged `autonomous` are all allowed; against a write-enabled `http` CMS both
   `guarded` and `autonomous` are refused unless `KEN_AUTONOMY_ALLOW_PRODUCTION=true` is also set;
   `autonomous` is refused unacknowledged anywhere; the legacy `KEN_AUTO_APPROVE_ENABLED=true` is
   still refused. A tightening, not a loosening.
2. **A deferral is not a block.** *(Superseded in part: the status it names is wrong. See "Corrections after integration" below, item 1: a deferral is recorded as `blocked` with an empty block list, not `awaiting_approval`.)* The three-decision vocabulary has no "defer", yet `guarded` mode
   must keep deferring a merely-too-risky plan (§3 keeps `awaiting_approval` for exactly this).
   `decision_for` returns `BLOCK` both for a fired hard block and for a guarded plan that is too
   risky, so the contract adds `target_status_for` to keep them apart: a deferral records
   `awaiting_approval`, a real block records `blocked`. Without it the deferral path would be
   silently deleted.
3. **`blocked` is deliberately not an open status.** Holding the (source, target) pair and a slice of
   the target's diversity budget for ever would be a leak. The per-pair cool-down in `publish_attempts`
   is the churn guard instead.
4. **The block evaluator is synchronous by contract.** `DeterministicBlockEvaluator.evaluate` is a
   plain function, so code with no I/O cannot smuggle in a model call; the 15-member `BlockCode`
   enum equals `DETERMINISTIC_BLOCK_CODES`.
5. **The operator override reuses `deploy`**, not a new permission: it publishes or withdraws one
   link, which is exactly what ADR-0014 gave `deploy`. `review` would be wrong in the other
   direction — a reviewer may approve a proposal, not write to the live site.
6. **The frozen-enum-copy trap, found in five places.** Migration 0010 replaces literal copies of
   enums that the new statuses and job kinds would otherwise violate: `ck_job_runs_kind_valid`
   (0003), the `link_plans` / `link_plan_transitions` status CHECKs and two partial-unique-index
   predicates (0004), the `review_actions` status CHECKs (0006), and the body of
   `ken_link_plan_status_guard()` (0007). A parametrised test asserts each now lists the new values.
7. **Open: a database CHECK for `critic_outcome = no_action` with a `BLOCK` decision.** The contract
   refuses the combination; the schema does not. Closing it is one line in 0010, deferred to the final
   integration pass so it does not invalidate the test databases of workstreams already running.

## Corrections after integration (2026-10-17)

Written once the code existed and was read back against this ADR. Where this section and the
text above disagree, **this section is right.**

1. **A guarded deferral is recorded as `blocked`, not `awaiting_approval`.** Amendment 2 above and
   section 3 said otherwise. Migration 0010's `ken_autonomy_decision_guard()` maps every `block`
   decision to `blocked`, and the directive's operational vocabulary has no "deferred" state, so the
   orchestrator decided the database guard wins. A deferral is `decision = BLOCK` with an **empty
   block list**, exposed as the explicit property `AutonomyEvaluation.deferred` (BLOCK, no fired
   code, not shadow, not a critic rejection), with the reason written in words on the plan's
   `blocked` transition note. `target_status_for` returns `BLOCKED` for it. An operator can still
   publish-override a deferral; a hard block is never overridable while its code still fires.
2. **Amendment 7 is closed, and more strictly than proposed.** The database now has
   `ck_autonomy_decisions_no_action_never_decided` (`critic_outcome <> 'no_action'`), so no decision
   row exists for `NO_ACTION` at all. The contract validator only forbade it combined with `BLOCK`.
3. **`guarded` additionally requires a calibrated decision class.** Section 2 says `guarded`
   publishes `AUTO_PUBLISH` decisions that are low-risk and high-confidence. The engine also requires
   a calibrated class (a passing, fresh evaluation with a row for that exact class, at least 30
   samples and a recorded calibrated confidence), matching ADR-0011's rule. The consequence is
   stated plainly: **without real review history no plan reaches plain `AUTO_PUBLISH`.** Every
   class is uncalibrated, so every plan ends in `AUTO_PUBLISH_WITH_EXTRA_VERIFICATION` or `BLOCK`,
   and `guarded` publishes nothing.
4. **Thresholds live in policy, not code.** `policies/autonomy/autonomy.v1.yaml` (guarded floor
   0.85, autonomous floor 0.50, retry 3 attempts and a 3600 s cool-down, and the risk thresholds)
   with contract-level floors the file cannot go below (guarded 0.75, autonomous 0.50, at most 5
   attempts, cool-down at least 600 s). The values were **not tuned to the demo corpus**: the best
   pairs there score about 0.65, so the shipped guarded floor publishes nothing on it even for a
   perfectly calibrated class, and a test asserts that.
5. **Audit identity of an autonomous publish.** `deployments.actor` must be a `user:` account, so
   the autonomous run acts as a named operator (`make autonomy-run AS_USER=<name>`). An auditor tells
   an autonomous publish from a human one by three links: `publish_attempts.deployment_id` joins to an
   attempt whose actor is `system:autonomy-engine`; the deployment `note` reads
   `autonomous publish (<decision>) decision#<id>`; and the plan went `autonomy_decided -> validated`
   where a human deploy goes `approved -> validated`. The attempt row is written after the CMS call,
   so a crash between the two loses it (links 2 and 3 survive).
6. **The agent hand-off ends at `policy_checked`, not `awaiting_approval`** (ADR-0019). Otherwise
   accepted plans never reached the engine. A plan whose verdict is `uncertain` stays `proposed`,
   recorded `handed_off = false`. The agent *run* status `awaiting_approval` is unchanged and now
   means "handed on".
7. **Fixed: `critic_outcome_of` no longer fails open.** It used to map a historic, handed-on
   `uncertain` review to `ACCEPT` with the risk tier raised and extra verification set, which
   contradicted ADR-0019's rule that an `uncertain` answer never maps to accept. Only rows written
   before the critic change could take that path, so a fresh installation never reached it, but a
   fail-open mapping on a safety path is not acceptable even for legacy data. It was changed to
   `NO_ACTION` always (the engine raises `CriticDeclinedError`, writes no decision row, and the
   tier-raise and extra-verification special cases for `uncertain` were removed), in the
   workstream B follow-up that implemented ADR-0019's mapping in the engine. The pure reference
   mapping is `ken_agents.critic.critic_outcome`; the engine's function may only be stricter, and a
   parametrised test over every verdict and both `handed_off` values pins that, including the legacy
   row.
8. **`KEN_VECTOR_STORE` defaults to `memory`.** See the amendment to ADR-0017.

