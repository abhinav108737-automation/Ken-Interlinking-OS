"""ken_core.providers.vectors -- vector stores behind the frozen VectorStore protocol.

``memory`` (exact cosine in process, the default) and ``qdrant`` (the local Qdrant service,
resolved by name by ``factory.py`` because it is a separate workstream). ADR-0017: the store
is a DERIVED index; PostgreSQL keeps the embedding values and stays the source of truth.
"""

from ken_core.providers.vectors.factory import build_vector_store
from ken_core.providers.vectors.memory import MemoryVectorStore

__all__ = ["MemoryVectorStore", "build_vector_store"]
