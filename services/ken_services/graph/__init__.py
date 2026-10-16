"""Internal link-graph intelligence (final-completion directive section 5).

Deterministic metrics computed in application memory from PostgreSQL and persisted to
``page_graph_metrics``: degrees, internal PageRank, hub/authority, crawl depth, orphan and
nearly-orphan status, bridge pages, connected components and topical clusters. No Neo4j and no
mandatory GNN; a graph-learning model may only sit behind :mod:`ken_services.graph.experiments`.
"""
