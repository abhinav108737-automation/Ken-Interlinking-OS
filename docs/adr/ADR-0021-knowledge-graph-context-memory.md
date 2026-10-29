# ADR-0021 — Knowledge graph, context packs and partial memory updates

- **Status:** Proposed, 2026-10-29. **Nothing here is implemented.**
- **Decision owner:** the project owner (`abhinav.kumar`).
- **Authority:** owner request of 2026-10-29: "introduce a knowledge graph where everything is
  stored — what is the input and what is the output; a context window where every context is
  stored; memory updated based on decisions, not overriding the whole memory, only the part needed
  to improve the output."
- **Relates to:** ADR-0010 (content intelligence), ADR-0012 (agent orchestration), ADR-0016
  (learning), ADR-0017 (Qdrant), ADR-0018 (autonomy). Amends none of them.

## Context: most of this already exists

Inspected in the running database and the contracts on 2026-10-29. The owner's three requirements
map onto tables that are already built, migrated and populated:

### a. Knowledge graph — exists, has no unified read surface

| Graph element | Where it already lives |
|---|---|
| Nodes: pages, immutable versions | `pages`, `page_versions` |
| Node attributes: type, intent, market, topics, entities, keywords, summary | `page_analyses` (immutable, versioned by `analysis_version`) |
| Typed edges with weight **and evidence** | `page_relationships` (`relationship_type`, `weight`, `evidence` jsonb) |
| Graph position: pagerank, hub/authority, in/out degree, orphan, bridge, cluster | `page_graph_metrics` |
| Semantic edges | Qdrant vectors + `page_embeddings` |
| Decisions (the output) | `link_plans` with `score`, `reason_codes`, `rationale` |
| Human decisions | `review_actions` (action, reasons, note, before/after anchor and target) |
| Published state and outcomes | `deployments`, `link_events`, `page_metrics_daily` |
| Everything, append-only | `events` (`event_name`, `subject_type`, `subject_id`, `payload`, `correlation_id`) |

**A property graph in Postgres already exists.** What is missing is one read API that returns it as
nodes and edges for a given page or topic.

### b. Context window — exists per agent run, missing its input snapshot

`agent_runs` plus `agent_steps` already store, per step: `node`, `status`, `attempt`, timings,
`model_role`, `model`, `fallback_used`, `fallback_reason`, `error_code`, `tool_calls` (jsonb),
`output` (jsonb), `trace_id`, `span_id`. `agent_run_plans` ties a run to the plans it reviewed.

**The outputs of every step are stored. What is not stored as one record is the input**: the exact
candidate set, policy versions and memory entries that were in force when the step ran.

### c. Partial memory update — exists, and is deliberately gated

ADR-0016 already implements "update a part, never the whole":

- `skills` are **versioned rows** (`name`, `version`, `status`, `source`, `proposal_id`,
  `evaluation_run_id`, `promoted_by`): a new version supersedes one named skill, nothing else.
- `learning_proposals` + `learning_proposal_transitions` scope a change to one decision class.
- `policy_evaluations`, `experiments`, `experiment_assignments` hold the evidence.
- Policy files are immutable and versioned (`recommendation.v1.yaml`); a v2 is a new file.
- Attribution already exists on every plan: `policy_version`, `ranking_policy_version`,
  `engine_version`, `analysis_version`, `embedding_model`, `reasoning_model`.

It produces nothing today only because reflection needs 30 samples per decision class
(`min_sample_count: 30`) and five reviews exist.

## Decision

### 1. No new graph database

The graph stays in PostgreSQL. Neo4j or similar would be a new infrastructure service (an ASK
action under CLAUDE.md), add hosting and failure modes, and duplicate the system of record. Postgres
is the system of record (constraint 4); a graph store would become a second one.

Revisit only if a traversal the product needs cannot be expressed in SQL. None is known today.

### 2. Three additions, in this order

**Addition 1 — Graph read API and screen (read-only, no schema change).**
`GET /graph/pages/{page_id}` returns the neighbourhood as `{nodes, edges}`: the page, its topics,
its typed relationships, its inbound and outbound link plans by state, and its graph metrics. Every
edge carries `kind`, `weight`, `evidence` and the `run_id` that produced it. Pure projection of
existing tables, so no migration. Tests required (CLAUDE.md 14).

**Addition 2 — Context packs (one new table).**
`context_packs`: `id`, `run_id`, `step_seq`, `subject_type`, `subject_id`, `inputs` jsonb,
`memory_refs` jsonb, `policy_refs` jsonb, `output_ref`, `content_hash`, `created_at`. Written by the
agent runner before a step and never updated (append-only, like `page_analyses`).

- `inputs`: the candidate set, retrieved neighbours, the page facts actually shown to the step.
- `memory_refs`: `[{skill_name, version}, …]` — exactly which memory entries were in force.
- `policy_refs`: the policy versions in force.
- `output_ref`: the `agent_steps` row that resulted.

This makes every decision reproducible: input, memory, policy, output, all addressable. It is the
"context window" requirement, and it is also what the learning pipeline needs to attribute an
outcome to a memory version rather than to the system as a whole.

**Addition 3 — Decision-scoped memory updates (no new mechanism).**
Reuse `learning_proposals` and `skills`. A reviewer decision produces a **proposal** scoped to one
decision class, carrying the `context_pack` ids that justify it. Promotion writes **one new skill
version** or one new policy file. The rest of memory is untouched by construction.

Two gaps to close, both small:
- **a. Structured reasons.** The four rejections of 2026-10-28 were filed as `other` with free
  text, which reflection cannot read. Propose adding `anchor_too_generic` to `FeedbackReason`
  (a contract change, hence this ADR). Without it, this feedback class stays invisible to learning.
- **b. Sample floor.** `min_sample_count: 30` stands. The learning policy forbids lowering it, and
  this ADR does not lower it. Until 30 reviews exist per class, memory updates are **proposals a
  human reads**, not automatic changes. That is the intended behaviour, not a defect.

### 3. What is explicitly refused

- No agent writes to `skills`, `learning_proposals` status, or any policy file. Promotion stays a
  human act (CLAUDE.md 16, ADR-0016).
- No "whole memory" object that a run can rewrite. Memory is addressable rows, versioned per name.
- No lowering of the sample floor, and no waiving of an eval suite, to make learning appear sooner.
- No LLM-written page content (ADR-0020 governs copy edits; this ADR adds nothing there).

## Consequences

- One migration (`context_packs`), one contract addition (`FeedbackReason.ANCHOR_TOO_GENERIC`),
  one read API with tests, one UI screen. The learning mechanism is unchanged.
- Reproducibility: any plan can be replayed from its context pack.
- Storage: context packs duplicate candidate data per run. Retention is needed (proposal: keep full
  packs 90 days, then keep refs and hashes only). **(verify against the Phase 7 retention purge.)**

## Risks

- **Context packs can hold a lot of text.** Cap `inputs` size and store references where possible.
- **Attribution is not causation.** Linking an outcome to a memory version does not prove the memory
  caused it; the Phase 7 no-causal-claim rule applies to any report built on this.
- **Feedback of one reviewer.** Five reviews, one person, demo content. Memory built on it would
  encode one person's preference as system behaviour. The sample floor is the mitigation.

## Open questions for the owner

1. Approve Postgres over a new graph database?
2. Approve the contract addition `anchor_too_generic`?
3. Context-pack retention: 90 days full, then references only?
