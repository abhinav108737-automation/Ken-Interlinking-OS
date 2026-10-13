# tools/ — reserved (arrives Phase 3)

Will hold `ken_tools`: the narrow, typed tools that agents are given instead of raw infrastructure.

**Nothing here in Phase 0.** No agent exists yet, so no tool exists yet.

## The rule these tools exist to enforce

CLAUDE.md, Agent Design Rule: *agents receive tools, not raw infrastructure credentials.* Every tool must
be **narrow, typed, allowlisted, logged and permission-aware**, and testable in isolation.

Planned examples (agent-runtime-engineer.md): `get_page`, `retrieve_candidates`, `get_graph_neighbors`,
`validate_destination`, `score_candidate`, `create_link_plan`, `submit_for_approval`.

What an agent will **never** be given: a general shell, an arbitrary-SQL tool, a raw database session,
a `CMSProvider` instance, or any credential. Writes go through permission-checked tools whose actions
map onto AUTONOMY_AND_PERMISSIONS.md (e.g. `WRITE_LINK_PRODUCTION` is ASK).

When this package is created it becomes a fifth package root in `pyproject.toml` (ADR-0001).
