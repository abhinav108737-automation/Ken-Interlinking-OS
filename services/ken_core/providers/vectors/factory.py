"""Build the configured :class:`~ken_contracts.vectors.VectorStore` (ADR-0017 section 3).

FROZEN SIGNATURE: ``build_vector_store(settings) -> VectorStore``, mirroring
``build_cms_provider``. ``KEN_VECTOR_STORE``:

* ``memory`` (default) -- :class:`~ken_core.providers.vectors.memory.MemoryVectorStore`,
  exact cosine in process, so tests and CI need no container;
* ``qdrant`` -- ``QdrantVectorStore`` over the local Qdrant HTTP API, resolved LAZILY BY NAME
  because the ``qdrant`` module is a separate workstream. Until it lands, selecting it raises
  :class:`~ken_contracts.vectors.VectorStoreNotConfiguredError` with a message that says so
  -- it never silently degrades to the in-memory store, because a silent fallback would make
  a production deployment think it had a shared index when it had a per-process one.

Each call returns a NEW store: build one at application startup and ``aclose()`` it at
shutdown.
"""

from __future__ import annotations

import importlib
from typing import Any, Final, assert_never

from ken_contracts.vectors import VectorStore, VectorStoreNotConfiguredError
from ken_core.config import Settings
from ken_core.providers.vectors.memory import MemoryVectorStore

QDRANT_MODULE: Final = "ken_core.providers.vectors.qdrant"

__all__ = ["QDRANT_MODULE", "build_vector_store"]


def build_vector_store(settings: Settings) -> VectorStore:
    """Return the vector store selected by ``KEN_VECTOR_STORE``."""
    match settings.vector_store:
        case "memory":
            return MemoryVectorStore(collection=settings.qdrant_collection)
        case "qdrant":
            try:
                module: Any = importlib.import_module(QDRANT_MODULE)
            except ModuleNotFoundError as exc:
                if exc.name != QDRANT_MODULE:
                    raise
                raise VectorStoreNotConfiguredError(
                    "KEN_VECTOR_STORE=qdrant, but the Qdrant store is not installed in this "
                    "build; set KEN_VECTOR_STORE=memory or install the Qdrant workstream"
                ) from None
            store: VectorStore = module.QdrantVectorStore(
                url=settings.qdrant_url,
                collection=settings.qdrant_collection,
                api_key=settings.qdrant_api_key,
                timeout_s=settings.qdrant_timeout_s,
            )
            return store
        case _:  # pragma: no cover - exhaustive over the Literal
            assert_never(settings.vector_store)
