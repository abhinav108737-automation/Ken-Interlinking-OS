"""Phase 5 local authentication (ADR-0013): scrypt password hashes, opaque bearer sessions.

* :mod:`.passwords` -- ``hashlib.scrypt`` hashing, constant-time verification, re-hash check;
* :mod:`.provider`  -- :class:`~.provider.LocalAuthProvider`, the PostgreSQL-backed
  :class:`~ken_contracts.auth.AuthProvider`;
* :mod:`.cli`       -- user administration (``python -m ken_services.auth.cli``), the ONLY
  way accounts are created or changed. There is no HTTP endpoint for it.
"""
