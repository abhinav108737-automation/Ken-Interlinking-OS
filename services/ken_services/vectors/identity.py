"""The ONE embedding identity an index run writes and a search reads (protocol.py rules).

Vectors of two models, or of two text recipes, are never comparable. The identity is therefore
resolved in one place, from PostgreSQL's own data when no model provider is reachable, and
shared by the indexer, the rebuilder, the verifier and the SEMANTIC channel so that none of them
can disagree about which vectors belong together.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from ken_contracts.vectors import VectorFilters, VectorPoint
from ken_core.config import Settings
from ken_core.db.repositories import intelligence as repo
from ken_services.intelligence.embeddings import current_recipe_fingerprint

__all__ = ["VectorIdentity", "page_version_id", "point_from_embedding", "resolve_identity"]


@dataclass(frozen=True, slots=True)
class VectorIdentity:
    """Provider-reported model + text recipe (``recipe_fingerprint`` embeds the version)."""

    model: str
    recipe_version: int
    recipe_fingerprint: str

    @property
    def embedding_version(self) -> str:
        """The ``VectorPoint.embedding_version``: the recipe identity, not a model tag."""
        return self.recipe_fingerprint

    def filters(self, *, exclude_page_ids: tuple[uuid.UUID, ...] = ()) -> VectorFilters:
        return VectorFilters(
            embedding_model=self.model,
            embedding_version=self.embedding_version,
            exclude_page_ids=exclude_page_ids,
        )


def page_version_id(page_id: uuid.UUID, version: int) -> str:
    """``<page_id>:<version>`` -- the contract's textual page-version identity."""
    return f"{page_id}:{version}"


def point_from_embedding(row: repo.CurrentEmbedding) -> VectorPoint:
    """The ONE place a ``page_embeddings`` row becomes a point (so the indexer, the rebuilder
    and the verifier cannot disagree about what a point should be)."""
    metadata: dict[str, str] = {}
    if row.page_type:
        metadata["page_type"] = row.page_type
    if row.language:
        metadata["language"] = row.language
    return VectorPoint.build(
        page_id=row.page_id,
        page_version_id=page_version_id(row.page_id, row.page_version),
        content_hash=row.input_hash,
        embedding_model=row.model,
        embedding_version=row.recipe_fingerprint,
        vector=row.vector,
        metadata=metadata,
    )


async def resolve_identity(
    session: AsyncSession,
    settings: Settings,
    *,
    model: str | None = None,
) -> VectorIdentity | None:
    """The identity to index / search, or ``None`` when no page is embedded yet.

    ``model`` (provider-reported, when a provider answered) wins; otherwise the model with the
    most current-version rows for the CURRENT recipe fingerprint is used. Never the raw
    ``KEN_MODEL_EMBEDDING`` setting string.
    """
    fingerprint = current_recipe_fingerprint(settings)
    if model is not None:
        from ken_contracts.intelligence import EMBEDDING_RECIPE_VERSION

        return VectorIdentity(
            model=model, recipe_version=EMBEDDING_RECIPE_VERSION, recipe_fingerprint=fingerprint
        )
    found = await repo.dominant_embedding_identity(session, recipe_fingerprint=fingerprint)
    if found is None:
        return None
    return VectorIdentity(model=found[0], recipe_version=found[1], recipe_fingerprint=found[2])
