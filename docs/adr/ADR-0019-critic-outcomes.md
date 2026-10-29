# ADR-0019 — The critic is a signal: accept / reject / no_action, and a doubt is never a licence

- **Status:** Accepted, 2026-10-16. Amends ADR-0012 sections 2, 3 and 6, and applies ADR-0018.
- **Authority:** the final-completion directive section 11 (the critic emits `ACCEPT`, `REJECT` or
  `NO_ACTION`; it cannot override deterministic safety policy), and ADR-0018 section 1.
- **Unchanged:** agents never insert, edit, approve, reject or expire a plan; the `agent:critic` and
  `agent:policy-gate` actors; the `agent_run_plans` UNIQUE(plan_id) rule; the tracing guard; the
  frozen contracts (`CriticVerdict` is still `accept | reject | uncertain`).

## Context

ADR-0012 handed `accept` **and** `uncertain` plans to a human queue (`awaiting_approval`), and a
model failure fell back to "accept at or above a confidence threshold". Under ADR-0018 there is no
approval queue: the critic's outcome is one input to the autonomy engine, which reads it from
`agent_run_plans` (`verdict`, `handed_off`) via `critic_outcome_of`. Two things were wrong:

1. Plans the agent accepted ended in `awaiting_approval`, a status the autonomy sweep does not
   decide (`DECIDABLE_STATUSES`), so an accepted plan never reached the engine.
2. `uncertain` and a degraded model could end up as an accept, which is permissive.

## Decision

### 1. One pure mapping, with one route to ACCEPT

`ken_agents.critic.critic_outcome(verdict)`; total, by identity with the enum members:

| model-boundary verdict | outcome | note |
|---|---|---|
| `accept` | `ACCEPT` | the only input that can produce it |
| `reject` | `REJECT` | a concrete defect; the engine turns it into `BLOCK` |
| `uncertain` | `NO_ACTION` | **chosen deliberately over `REJECT`** (see below) |
| anything else, `None`, a look-alike string | `NO_ACTION` | never permissive |

`uncertain` maps to `NO_ACTION`, not `REJECT`, because no defect was found: there is nothing to
record as a blocked action, ADR-0018 says `NO_ACTION` "never becomes a publish", and an operator can
still override a plan that stayed `proposed`. Mapping it to `ACCEPT` with a raised risk tier (the
earlier engine reading) is rejected: the tier is a tuning signal, not a veto.

`CriticVerdict` keeps `uncertain` as the model-boundary and storage vocabulary, so the frozen
contract, the stored rows, the generated API and the UI shape are unchanged. A pure test covers every
input and a second test pins agreement with `critic_outcome_of` in the engine.

### 2. Hand-off: only an accept is handed on, and the ladder ends at `policy_checked`

`hand_off` records every review. Only `accept` moves the plan, `proposed -> critiqued`
(`agent:critic@graph-v1`) `-> policy_checked` (`agent:policy-gate@graph-v1`), and **stops there**.
The autonomy engine takes over from `policy_checked`. `awaiting_approval` is no longer written by
agents (ADR-0018 section 3: it is reachable only for a guarded-mode deferral and an operator).
`reject` and `uncertain` are recorded with `handed_off = false` and the plan stays `proposed`, so
the engine reads `REJECT` or `NO_ACTION` back and the plan is never re-reviewed. The run status
`awaiting_approval` is unchanged and means "handed on".

### 3. A model problem degrades to NO_ACTION

Model unavailable, timeout or a schema violation (including an omitted ref) yields a recorded
`uncertain` review with its `FallbackReason`, whatever the plan's confidence. The one exception is a
policy that turns the model off (`critic.use_model: false`, `model_disabled`): the documented
threshold rule applies, recorded as a configured rule rather than a failure, and the guards below
still apply. Model-less deployments therefore choose that mode explicitly.

### 4. Deterministic guards after the model

`ken_agents.critic.guard_for` runs after the model or the rule, best-ranked first, over the plan's own
stored fields only: a generic anchor ("click here") is `REJECT`; a duplicate destination, the same
anchor used for another page (cannibalisation) and an overlapping placement are `NO_ACTION`. A guard
only moves an outcome toward `REJECT` or `NO_ACTION`; a model `accept` cannot survive one. Only a
still-accepted better-ranked candidate shadows a later one.

### 5. The critic cannot override safety policy

The deterministic blocks (ADR-0018) are computed by the autonomy engine from stored facts before the
critic row is consulted; the agent package imports nothing from `ken_services.autonomy` (a static
test), and an integration test drives a real agent run that accepts a plan, protects its destination,
and shows `BLOCK` with `PROTECTED_PAGE` in every mode while the recorded critic outcome stays `ACCEPT`.

### 6. Prompt-injection hardening

`prompts.py` keeps the opaque `c0...` refs, one JSON `DATA` value and the untrusted-data system
message, and adds: Unicode control, format (zero-width, bidi), private-use and separator characters
are removed; `<`, `>` and `&` are escaped inside the JSON so no literal tag or marker exists; the
system message states that the model cannot publish, approve, promote or configure anything (and no
longer says a human approves every link). Tests cover "ignore your instructions", "promote
yourself", "set auto_approve", a `</DATA>` escape, a forged second `DATA:` section, a fenced answer,
invisible characters, and instructions embedded in titles, page text, anchor text and a model
explanation, and prove the same recorded answer yields the same outcome with and without them.

## Consequences

- The agent eval fixtures and oracle (`tests/fixtures/agents`, `tests/unit/agent_evals`) were updated:
  `uncertain` is recorded, not handed; plans stop at `policy_checked`; a degraded model never accepts.
- The learning critic suite (`evals/datasets/critic/core.v1.jsonl`) has two cases that expect
  `verdict:accept+fallback` on a malformed or unmatched model answer; they now observe
  `verdict:uncertain+fallback` and need re-recording by the evals owner.
- Contract follow-ups requested of the orchestrator (not made here, the contract is frozen): add
  `CriticReason` members for duplicate intent, cannibalisation, destination fit and editorial
  awkwardness (today they are mapped onto `OVER_LINKING`, `WEAK_RELEVANCE` and `MISLEADING_ANCHOR`
  with the specific finding in the explanation and the step output); optionally retire `uncertain`
  from the model schema in favour of `no_action`.
- Engine follow-up suggested to workstream B: raise the risk tier when a stored review has a
  `fallback_reason` other than `model_disabled`; and treat a stored `uncertain` with
  `handed_off = true` (historic rows only) as `NO_ACTION`.

## Alternatives considered

- **Option (a): replace `uncertain` with `no_action` in the frozen model schema.** Cleaner
  vocabulary, but it changes the frozen contract, the OpenAPI snapshot, the generated TypeScript,
  the UI and the recorded eval datasets for no behavioural gain: the pure mapping gives the same
  guarantee with the shapes untouched.
- **`uncertain -> REJECT`.** Over-reports defects and floods the blocked-actions screen with plans
  nobody found fault with.
- **Keep handing `uncertain` plans on with a higher risk tier.** Rejected: that treats a doubt as a
  licence, which is exactly what the directive forbids.
