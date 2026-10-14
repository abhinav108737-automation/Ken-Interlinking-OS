"""Phase 3 recommendation engine (ADR-0011): retrieve, filter, score, place.

The deterministic core is pure (no database, no network, no model):

* :mod:`.policy`     -- validated, cached loaders for the recommendation and ranking policies;
* :mod:`.filters`    -- the ADR section 3 'pair' stage (:func:`~.filters.pair_rejection`);
* :mod:`.scoring`    -- the ADR section 4 transparent score and its reason codes;
* :mod:`.placement`  -- the ADR section 5 anchor phrases and natural, never fabricated, placement;
* :mod:`.freshness`  -- the ADR section 6 fresh / rebasable / stale check;
* :mod:`.rationale`  -- deterministic rationale sentences rendered from reason codes.

Import from the submodules; this package re-exports nothing, so importing one module never
drags in the database-facing pipeline.
"""
