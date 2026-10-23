# Autonomy engine

Reference for engineers who will connect the system to a real CMS. Every statement was checked against the module cited; unverified items say so. Governing decisions: ADR-0018 (including its contract-freeze amendments), ADR-0019, ADR-0014. Where an ADR and the code differ, this document follows the code.

## Where it sits

The engine (`services/ken_services/autonomy/engine.py`, `DatabaseAutonomyEngine`) decides and records. It never writes to the CMS. The publisher (`autonomy/publish.py`, `AutonomousPublisher`) writes, and only for a decision the engine took. Both are CLI-driven (`autonomy/cli.py`); no UI button starts a pass. The API has read routes plus one operator-override write route.

## The three decisions and the facts that reach each

Order of evaluation in `engine.py` (`_gather`, `_decide`, `_apply_floors`):

1. Critic outcome `no_action`: the plan is not evaluated at all (`CriticDeclinedError`; a database CHECK forbids a decision row for it).
2. Deterministic blocks (`autonomy/blocks.py`) from stored facts. Any code means `BLOCK`.
3. Critic `reject` means `BLOCK`.
4. Calibrated confidence (`autonomy/calibration.py`): can only lower the raw value.
5. Risk tier (`autonomy/risk.py`): can only rise.
6. `decision_for` (`autonomy/protocol.py`), the single mapping from blocks, mode, tier and extra-verification flag.
7. Mode floors and rollback availability: can only block.

| Decision | Reached when |
|---|---|
| `BLOCK` | any block code; critic reject; mode `shadow`; `guarded` and tier not low; confidence under the mode's floor; `guarded` and not calibrated; no rollback metadata |
| `AUTO_PUBLISH_WITH_EXTRA_VERIFICATION` | everything above passed and any of: class not calibrated, tier high, `strict` configured |
| `AUTO_PUBLISH` | everything passed and the class is calibrated, tier is not high, `standard` configured |
| no decision (the plan is declined) | the critic review is `uncertain`, failed or unparseable, or an accept the run did not hand on: `NO_ACTION`, see below |

Plain `AUTO_PUBLISH` needs a calibrated decision class. A class is calibrated only if a `policy_evaluations` run passed its gate, finished within the learning policy's maximum evaluation age, carries a row for that exact class with `sufficient` set and a sample count at or above the learning policy's `min_sample_count`, and records a `calibrated_confidence` (`EvaluationCalibrationReader.lookup`). Without that history every plan has `calibration_version = NULL`, so plain `AUTO_PUBLISH` is unreachable. `tests/integration/test_autonomy_reachability.py` proves this over the demo corpus: with no history only `AUTO_PUBLISH_WITH_EXTRA_VERIFICATION` or `BLOCK` occur. The extra-verification decision differs from plain only in the verification profile, never in the safety rules.

`engine.py` `critic_outcome_of` fails closed and agrees with `ken_agents.critic.critic_outcome`, which it may only be stricter than: `reject` is REJECT; `accept` is ACCEPT unless the run did not hand it on, then NO_ACTION; `uncertain` is NO_ACTION **always**, including a historic row recorded as handed on (current agents never hand those on, ADR-0019); an unknown verdict is NO_ACTION; no critic row at all is ACCEPT, because the deterministic layer and not the critic is what makes a plan safe. NO_ACTION makes the engine raise `CriticDeclinedError`: no decision row is written (the database forbids one, `ck_autonomy_decisions_no_action_never_decided`), no tier is raised, nothing is published, and a sweep counts the plan as `declined`. An uncertain critic therefore does not raise the risk tier or demand extra verification; it is declined. `tests/unit/agents/test_critic.py` pins the agreement over every verdict and both `handed_off` values.

## Modes and defaults

`KEN_AUTONOMY_MODE` is `shadow` (default), `guarded` or `autonomous` (`services/ken_core/config.py`).

- `shadow`: records one `autonomy_decisions` row whose `decision` is `block` and whose `would_have_decision` holds what `autonomous` would have done. No deployment, no publish attempt, no plan transition. Migration 0010 enforces this with a deferred guard function and CHECKs (`shadow_never_publishes`, `counterfactual_iff_shadow`, `publish_attempts` `mode <> 'shadow'`).
- `guarded`: publishes only if the tier is low, the class is calibrated and calibrated confidence is at least `guarded_min_confidence`. Because tier and calibration both gate it, `guarded` can only produce plain `AUTO_PUBLISH`.
- `autonomous`: publishes every policy-eligible decision whose confidence is at least `autonomous_min_confidence`. Hard blocks still apply.

`--mode` on the CLI and `resolve_mode` may only make a run more conservative than the configured mode.

## The 15 deterministic block codes

`ken_contracts/autonomy.py` `BlockCode`, evaluated by the pure synchronous `evaluate_blocks` in `autonomy/blocks.py`: `self_link`, `external_destination`, `protected_page`, `unpublished_destination`, `broken_target`, `stale_source_version`, `duplicate_contextual_link`, `prohibited_page_type`, `policy_disallowed_destination`, `invalid_canonical`, `unsupported_redirect`, `invalid_page_state`, `invalid_anchor`, `missing_rollback_metadata`, `stale_vector`. All fired codes are accumulated, in enum order.

No model can clear one. The evaluator is a plain function with no I/O, clock, randomness or model provider; `decision_for` returns `BLOCK` on a non-empty list before looking at anything else; a CHECK (`blocks_force_block`) refuses a record that claims otherwise; and static tests assert the import graph (`tests/unit/autonomy/test_block_determinism.py`). `stale_vector` is decided from PostgreSQL embedding rows at the current page versions, not from a live Qdrant query. An operator publish override re-evaluates the blocks and is refused while any code fires.

## Guarded deferral

`decision_for` returns `BLOCK` both for a fired block and for a `guarded` plan that is merely too risky, uncalibrated or under-confident. `target_status_for` sends both to `blocked`. A deferral is therefore recorded as `blocked` with an empty block list; `AutonomyEvaluation.deferred` (`ken_contracts/autonomy.py`) is true for a `BLOCK` with no codes, outside shadow, where the critic did not reject. The reason is written on the plan transition note (`autonomy/reasons.py`) and shown on the blocked-actions screen. The code does not use `awaiting_approval` for this: migration 0010's `ken_autonomy_decision_guard()` maps every `block` decision to `blocked`, and the docstring of `target_status_for` records that the database guard won. ADR-0018 amendment 2 still says a deferral records `awaiting_approval`, which is no longer true.

## Policy thresholds and floors

`policies/autonomy/autonomy.v1.yaml` is loaded by `autonomy/policy.py` with no settings field for its path. v1 values: `guarded_min_confidence` 0.85, `autonomous_min_confidence` 0.50, `min_positive_outcome_rate` 0.50, retry 3 attempts and 3600 s cool-down, hub threshold 20 inbound links, sensitive anchor 60 characters, one prior failure raises to high, `max_redirect_hops` 1, and the commercial page types. The contract defines floors a file cannot pass; a value beyond one fails validation and the process refuses to start (`ken_contracts/autonomy.py`): guarded confidence at least 0.75, autonomous at least 0.50, positive-outcome rate at least 0.50, at most 5 attempts, cool-down at least 600 s, hub threshold at most 50, anchor length at most 80, prior-failure threshold at most 2, redirect hops at most 1, and the seven commercial types cannot be removed. The policy version is stored on every decision. Changing the file means a new version; learning cannot change it.

## Configuration gates

Enforced at startup by `Settings._autonomy_is_deliberate` (`config.py`):

- `shadow`: always allowed, publishes nothing.
- `autonomous`: refused unless `KEN_AUTONOMY_AUTONOMOUS_ACKNOWLEDGED=true`.
- `guarded` or `autonomous` against a write-enabled `http` CMS (`KEN_CMS_PROVIDER=http` with writes enabled): refused unless `KEN_AUTONOMY_ALLOW_PRODUCTION=true`. Against the simulated CMS the gate does not apply.
- `KEN_AUTO_APPROVE_ENABLED` is retired and still refused when true.
- **Defence in depth.** The startup validator is the first line and can be bypassed (`Settings.model_construct`, a test, a future code path), so the RUNTIME gate is one shared function, `autonomy/gate.py` `publishing_refusal` / `require_publishing`. `AutonomousPublisher.publish_plan` calls it before it evaluates, records or sends anything, `_apply` calls it again immediately before the deployment service, and `run_autonomy` calls it before a job row exists. A closed gate raises `PublishingNotEnabledError` carrying a stable code (`cms_writes_disabled`, `production_not_allowed`, `autonomous_not_acknowledged`); the CLI maps it to a usage error. The engine's and the API reader's former copies of the rule are deleted. `tests/integration/test_autonomous_publish.py` proves zero CMS calls with a validator-bypassed `autonomous` + write-enabled `http` + no `allow_production`.
- A publishing pass needs `--as-user <username>` (`make autonomy-run AS_USER=...`): an enabled account with the `deploy` permission, because `deployments.actor` has a `^user:` CHECK.

## Verification, rollback, retry budget

`AutonomousPublisher.publish_plan` order: pair budget check, re-evaluate on current facts, deployment service VALIDATE and dry run, durable intent, one CMS call, verify, audit, automatic rollback if needed. The decision is retaken immediately before publishing, so a page protected in the meantime blocks it.

- Verification profile comes from the decision. Standard uses `deploy_verify_*` settings. Strict (`deployment/strict.py`, `deployment/service.py`) re-fetches, runs at least two passes with a delay (defaults 2 passes, 30 s), counts every link to the destination so a second unintended link fails, and requires the link to be located in the expected block.
- Verification failed: automatic rollback through `CMSProvider.rollback` (inverse splice on current HTML). Not attempted when the change never reached the page; refused when our link was itself changed (`rollback_conflict`), in which case the failure is recorded for a human.
- Inconclusive verification is recorded as `published_unverified`: not a failure, no cool-down, settled later by the verification sweep.
- Budget (`autonomy/budget.py`): per (source, target) pair, ever. Effective values are the stricter of the policy and `KEN_AUTONOMY_PUBLISH_MAX_ATTEMPTS` / `_COOLDOWN_S` (`effective_retry`). Once attempts reach the maximum the pair is never published again; a failure or rollback starts the cool-down. The check runs under the plan lock just before publishing and a unique index refuses a racing duplicate attempt. Known gap: a process killed between the CMS call and the attempt row loses one attempt (`publish.py` comment).

## Operator override

`autonomy/override.py`, `DatabaseOverrideService`, requires `Permission.DEPLOY` and a reason. `block` stops any not-yet-written plan. `publish` moves a `blocked` plan to `autonomy_decided` and is refused while any block code fires. It waives only the mode floors (confidence, calibration, tier); the next `make autonomy-run` still performs validation, the CMS write and verification, forced to the strict profile. It is idempotent by state and checks `expected_status` under the plan lock.

**The exception, stated plainly.** An operator may publish a plan the critic REJECTED, or one the engine merely DEFERRED (guarded mode: risk tier, confidence or calibration), but never a plan with a fired block code. A critic rejection is a local-model quality judgement, not a safety rule, and the override is an audited human act; the deterministic block codes are the hard stop and are re-evaluated both when the override is requested and again by the publisher immediately before the write. The override row records the REAL stored critic outcome and verdict (a `reject` stays `reject`; it never says the critic accepted), and the publish is forced to the strict profile. An override of a plan whose critic outcome is `no_action` (an `uncertain` review, fresh or legacy) is REFUSED with a typed reason rather than recorded: the database forbids a decision row with that outcome, and recording `accept` instead would falsify the trail. That applies to a `block` override too; reject such a plan through the review path.

## Telling autonomous from human

- Plan transitions and `autonomy_decisions` rows carry actor `system:autonomy-engine`; an override row carries `user:<name>` and a mandatory `override_reason`.
- `deployments.actor` is the run's operator account, so it cannot tell the two apart. The deployment note reads `autonomous publish (<decision>) decision#<id>`, and `publish_attempts` (actor `system:autonomy-engine`, `deployment_id` set) joins from the other side. A human deploy has neither.

## The critic as a signal

ADR-0019 and `agents/ken_agents/critic.py`: accept is the only route to ACCEPT; reject becomes a block; uncertain, unavailable or invalid becomes NO_ACTION and never a publish. The critic cannot clear a block; the engine computes blocks first from stored facts. Behavioural analytics can lower confidence or raise a tier but never justify a publish.

## What a first installation does

The default is `shadow`, the corpus has no evaluation history and every plan's confidence is raw.

- `make autonomy` records decisions and `would_have_decision` and changes no plan and no CMS page. Nothing publishes until an operator changes mode and acknowledgements.
- Under `guarded`, nothing publishes: an uncalibrated class is deferred regardless of score.
- Under acknowledged `autonomous`, plans with no block, no critic rejection, rollback metadata and raw confidence at or above 0.50 would publish with the strict profile and never as plain `AUTO_PUBLISH`. The v1 policy comments note the demo corpus scores its best pairs around 0.65.
- With the default critic policy (`use_model: true`) and no reachable model, every plan is NO_ACTION and nothing reaches the engine.
- Calibration cannot start without reviewer or outcome history, so plain `AUTO_PUBLISH` stays unreachable until the learning pipeline has produced a passed, sufficiently sampled evaluation. Not verified against a real CMS: all end-to-end evidence here is against the simulated CMS.
