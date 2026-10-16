"""Autonomy services (docs/adr/ADR-0018-autonomy-first-publishing.md).

Frozen interfaces live in :mod:`ken_services.autonomy.protocol`. The implementation modules
(``blocks``, ``risk``, ``calibration``, ``engine``, ``budget``, ``override``, ``reader``,
``cli``) land in the autonomy workstream and do not change the signatures there.

Two rules bind every module in this package, enforced by static tests (the
``ken_services/learning`` pattern):

1. **``blocks.py`` imports no model provider.** The deterministic block set must be
   computable with the model server switched off; that is the whole guarantee ADR-0018
   section 1 rests on.
2. **No module here contains a ``"user:"`` literal.** A human actor is only ever derived from
   an authenticated :class:`~ken_contracts.auth.Principal`, because the database can check an
   actor's shape but only a session proves it is a person.
"""
