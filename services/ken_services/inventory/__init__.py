"""Phase 1 content inventory: sources, normalisation, versioned persistence.

Module map (ownership in brackets):

* ``urls``         URL normalisation and site scope              [orchestrator, frozen]
* ``html``         shared HTML discovery: hrefs, canonical, robots [orchestrator, frozen]
* ``normalize``    SourceDocument -> NormalizedPage               [parser]
* ``hashing``      content hash                                   [parser]
* ``robots``       RFC 9309 robots.txt policy                     [crawler]
* ``sources.*``    ContentSourceProvider implementations          [parser / crawler]
* ``ingest``       run accounting, versioning, events             [backend]
* ``reader``       SQL implementation of InventoryReader          [backend]
* ``cli``          ``python -m ken_services.inventory.cli``       [backend]
"""
