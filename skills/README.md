# Skill Registry (application) — reserved

> **This is the APPLICATION's procedural-skill registry**: versioned operating
> instructions that the Ken interlinking agents use at runtime (PRD §9,
> TRD §17).
>
> **It is UNRELATED to Claude Code's `.claude/skills/` or `.claude/agents/`.**
> Those configure the AI assistant that *builds* this repository. Nothing here
> configures Claude Code, and nothing in `.claude/` is an application skill.
> Do not move files between them.

> **Phase 0 status: RESERVED. Empty by design.** The skill registry arrives in
> **Phase 8** (IMPLEMENTATION_PLAN.md: "skill registry, skill proposals").
> There is no loader, schema, table or runtime code in Phase 0.

## What a skill is

A skill is a reusable, versioned procedure that an agent node follows. PRD §9
gives these examples:

- `find_parent_market_links`
- `select_natural_anchor`
- `detect_overlinking`
- `repair_orphan_report`
- `evaluate_underperforming_link`

A skill is **instructions plus typed I/O**. It is not a credential, a tool
grant or a permission. Skills run through the same narrow, allowlisted tools
and the same Pydantic boundaries as every other agent step (CLAUDE.md, "Agent
Design Rule"; TRD §6).

## Skill record (future)

Required by TRD §17:

| Field | Notes |
|---|---|
| `name` | Stable identifier |
| `version` | **Every revision creates a new version.** Versions are never edited in place. |
| `description` | What the skill is for |
| `instructions` | The procedure itself |
| `input_schema` | Pydantic-validated input |
| `output_schema` | Pydantic-validated output (TRD §6) |
| `source` | Where it came from (human-authored or proposed from experience) |
| `evaluation_score` | Result of the offline eval that gated it |
| `status` | Lifecycle state (below) |

The eval-learning-engineer role adds evidence fields that every learned skill
must carry: **source evidence**, **evaluation dataset**, **baseline score**,
**candidate score** and **activation decision**.

TRD §5 lists a `skills` table. PostgreSQL is the system of record
(CLAUDE.md constraint 4). How files in this directory relate to that table is
**an open Phase 8 design decision** and needs an ADR. Whatever is decided, a
file existing here must never make a skill active by itself.

## Lifecycle

Spec flow (TRD §18):
`experience → reflection → proposal → evaluation → activation`.

Controlled flow, aligned with `evals/README.md`:
`experience → structured feedback → proposal → offline eval → shadow → canary → experiment → explicit promotion`.

Proposed status values (not in the specs; to be confirmed in Phase 8):
`PROPOSED → EVALUATING → SHADOW → ACTIVE`, or `REJECTED` / `RETIRED`.

## Hard rules

- **Skills can be proposed but never self-activated.** The Learning agent
  "produces skill/policy proposals only" (TRD §10). `PROPOSE_SKILL` is ALLOW.
- **Activation requires a passing evaluation AND explicit promotion.**
  `PROMOTE_SKILL` is **ASK**-level (AUTONOMY_AND_PERMISSIONS.md). A passing
  eval is necessary but never sufficient.
- **No agent may promote its own, or any, skill** (CLAUDE.md constraint 16).
- **No uncontrolled self-modification.** Learning never goes straight from
  experience to a changed production skill (TRD §18).
- Every proposal emits `skill.proposed` (TRD §15); every promotion emits
  `skill.promoted` (added 2026-10-13 by user decision). Lifecycle:
  `skill.proposed` → offline/human evaluation → `skill.promoted`. The event
  contract **rejects** a promotion unless its payload names the human
  `promoted_by` and the passing `evaluation_ref` (docs/CONTRACTS.md rule 11).
- Skills reference models by role (`fast` / `reasoning` / `embedding`), never
  by model name (CLAUDE.md, "Model Design Rule").
