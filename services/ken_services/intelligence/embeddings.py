"""Page embeddings: the shared identity helper and batch embedding of stale pages (ADR-0010 s.2).

**Identity.** An embedding is keyed by the PROVIDER-REPORTED model of the
EMBEDDING role (``ProviderCapabilities.roles[EMBEDDING].model``), together with
the provider name, the dimensions the provider returns and
``EMBEDDING_RECIPE_VERSION``. :func:`embedding_identity` is the one place that
resolves it; nothing uses the raw ``KEN_MODEL_EMBEDDING`` setting string.

**Batching.** :func:`embed_stale_pages` embeds every page whose CURRENT version
has no row for (identity.model, recipe version), ``settings.embedding_batch_size``
texts per call. Each batch is written in ONE transaction, so a batch is either
fully stored or not at all. A batch is refused (counted in
``embedding_failures``, nothing written) when the provider fails, reports a
different model than the identity, or returns vectors whose count or length
does not match. Vectors of two models are therefore never mixed.
"""

from __future__ import annotations

import importlib
import math
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from ken_contracts.intelligence import EMBEDDING_RECIPE_VERSION
from ken_contracts.model import (
    EmbeddingResult,
    ModelProvider,
    ModelRole,
    ModelUnavailableError,
)
from ken_core.config import Settings
from ken_core.db.repositories import intelligence as repo
from ken_core.logging import get_logger
from ken_services.jobs.runner import JobContext

__all__ = [
    "EMBEDDING_COUNTERS",
    "EmbeddingIdentity",
    "EmbeddingRecipe",
    "default_recipe",
    "embed_stale_pages",
    "embedding_identity",
]

logger = get_logger("ken_services.intelligence.embeddings")

EMBEDDING_COUNTERS: Final = ("embeddings_written", "embeddings_fresh_skipped", "embedding_failures")


@dataclass(frozen=True, slots=True)
class EmbeddingIdentity:
    """Who produced a vector: provider name + the model the PROVIDER reports."""

    provider: str
    model: str


async def embedding_identity(provider: ModelProvider) -> EmbeddingIdentity:
    """Resolve the current embedding identity from ``provider.capabilities()``.

    Raises:
        ModelUnavailableError: the provider reports no usable EMBEDDING model.
        ModelProviderError: the provider could not be asked (e.g. unreachable).
    """
    capabilities = await provider.capabilities()
    for status in capabilities.roles:
        if ModelRole(status.role) is ModelRole.EMBEDDING:
            if status.model and status.status != "missing":
                return EmbeddingIdentity(provider=provider.name, model=status.model)
            break
    raise ModelUnavailableError("the embedding role has no available model")


@dataclass(frozen=True, slots=True)
class EmbeddingRecipe:
    """The text recipe: ``text(...)`` builds the embedded string, ``digest`` hashes it."""

    text: Callable[..., str]
    digest: Callable[[str], str]


def default_recipe() -> EmbeddingRecipe:
    module: Any = importlib.import_module("ken_services.intelligence.embedding_text")
    return EmbeddingRecipe(text=module.embedding_text, digest=module.input_hash)


def _valid_result(result: EmbeddingResult, *, identity: EmbeddingIdentity, count: int) -> bool:
    if result.model != identity.model or len(result.vectors) != count:
        return False
    return all(
        len(vector) == result.dimensions and all(math.isfinite(v) for v in vector)
        for vector in result.vectors
    )


async def embed_stale_pages(
    *,
    job: JobContext,
    provider: ModelProvider,
    identity: EmbeddingIdentity,
    settings: Settings,
    recipe: EmbeddingRecipe,
    page_ids: Sequence[uuid.UUID] | None = None,
) -> set[uuid.UUID]:
    """Embed the current version of every page lacking a fresh embedding.

    Updates ``embeddings_written``, ``embeddings_fresh_skipped`` and
    ``embedding_failures`` on ``job``. Returns the ids of the pages embedded.
    """
    async with job.sessionmaker() as session:
        pages = await repo.list_pages(session, page_ids=page_ids)
        current = [(page.id, page.current_version) for page in pages]
        done = await repo.embedded_versions(
            session,
            model=identity.model,
            recipe_version=EMBEDDING_RECIPE_VERSION,
            pages=current,
        )
    stale = [pair for pair in current if pair not in done]
    job.increment("embeddings_fresh_skipped", len(current) - len(stale))

    written: set[uuid.UUID] = set()
    size = settings.embedding_batch_size
    for start in range(0, len(stale), size):
        batch = stale[start : start + size]
        try:
            items = await _embed_batch(
                job=job,
                provider=provider,
                identity=identity,
                settings=settings,
                recipe=recipe,
                batch=batch,
            )
        except ModelUnavailableError as exc:
            # The model is gone for this run: every remaining page fails, nothing is written.
            remaining = len(stale) - start
            logger.warning("embedding_unavailable", error_class=type(exc).__name__, pages=remaining)
            job.increment("embedding_failures", remaining)
            break
        except Exception as exc:  # ModelProviderError / timeout, or a misbehaving provider
            logger.warning(
                "embedding_batch_failed", error_class=type(exc).__name__, pages=len(batch)
            )
            job.increment("embedding_failures", len(batch))
            continue
        if items is None:
            job.increment("embedding_failures", len(batch))
            continue
        try:
            async with job.transaction() as tx:
                new = await repo.insert_embeddings(
                    tx.session, embeddings=items, run_id=job.run_id, created_at=job.clock()
                )
                tx.increment("embeddings_written", len(new))
        except Exception as exc:  # a storage failure costs this batch only
            logger.error(
                "embedding_batch_not_stored", error_class=type(exc).__name__, pages=len(batch)
            )
            job.increment("embedding_failures", len(batch))
            continue
        written |= new
    await job.flush()
    return written


async def _embed_batch(
    *,
    job: JobContext,
    provider: ModelProvider,
    identity: EmbeddingIdentity,
    settings: Settings,
    recipe: EmbeddingRecipe,
    batch: Sequence[tuple[uuid.UUID, int]],
) -> list[repo.NewEmbedding] | None:
    """Embed one batch; None (logged) when the result cannot be stored as-is."""
    contents: list[repo.VersionContent] = []
    async with job.sessionmaker() as session:
        for page_id, version in batch:
            content = await repo.load_version_content(session, page_id=page_id, version=version)
            if content is None:  # pragma: no cover - the FK makes this impossible
                return None
            contents.append(content)
    texts = [
        recipe.text(
            title=c.title,
            meta_description=c.meta_description,
            headings=tuple(text for _, text in c.headings),
            body_text=c.body_text,
            max_chars=settings.embedding_max_chars,
            prefix=settings.embedding_document_prefix,
        )
        for c in contents
    ]
    result = await provider.embed(texts=texts, role=ModelRole.EMBEDDING)
    if not _valid_result(result, identity=identity, count=len(texts)):
        # Never mix models: a provider answering with another model is refused.
        logger.warning(
            "embedding_result_rejected",
            identity_matches=result.model == identity.model,
            pages=len(batch),
        )
        return None
    return [
        repo.NewEmbedding(
            page_id=c.page_id,
            page_version=c.version,
            model=identity.model,
            provider=identity.provider,
            dimensions=result.dimensions,
            recipe_version=EMBEDDING_RECIPE_VERSION,
            input_hash=recipe.digest(text),
            vector=list(vector),
        )
        for c, text, vector in zip(contents, texts, result.vectors, strict=True)
    ]
