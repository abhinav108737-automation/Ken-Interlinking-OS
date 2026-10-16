"""Phase 8 learning services (docs/adr/ADR-0016-learning.md; section 15 binding).

Frozen interfaces live in :mod:`ken_services.learning.protocol`; the gate policy loader in
:mod:`ken_services.learning.policy`. The implementation modules (``aggregate``, ``reflect``,
``proposals``, ``dedupe``, ``evaluate``, ``lifecycle``, ``shadow``, ``promote``,
``experiments``, ``reader``) land in the parallel workstreams and do not change the
signatures here.

Two rules bind every module in this package, enforced by a static test
(``tests/unit/learning/test_boundaries.py``, the ``tests/unit/agents/test_boundaries.py``
pattern):

1. **No module here contains a ``"user:"`` literal.** A human actor is only ever derived from
   an authenticated :class:`~ken_contracts.auth.Principal` (ADR-0016 15.7.3).
2. **Only ``promote.py`` writes ``learning_proposal_transitions``.** One code path records a
   promotion, and it is the one that sets the transaction-local ``ken.learning_promoter``
   setting the database guard requires.
"""
