"""Agent runtime: the Phase 0 worker heartbeat and, from Phase 4, the LangGraph supervisor
(ADR-0012). ``langgraph`` is imported lazily by ``ken_agents.graph.build_graph`` only."""
