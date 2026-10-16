"""Vector indexing services (ADR-0017).

Frozen interfaces live in :mod:`ken_services.vectors.protocol`. The store itself is the
frozen :class:`ken_contracts.vectors.VectorStore`, built by
``ken_core.providers.vectors.factory.build_vector_store``; the implementation modules
(``indexer``, ``rebuild``, ``verify``, ``retrieval``, ``cli``) land in the vector workstream.

The rule that binds every module here: **PostgreSQL is the source of truth and the collection
is derived.** Nothing is ever read from the store and written back to PostgreSQL, so dropping
the collection costs a reindex and never data.
"""
