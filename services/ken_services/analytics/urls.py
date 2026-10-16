"""``InventoryUrlResolver``: URL -> ``str(pages.id)``, built ONCE per import (ADR-0015 §4).

The rules, in order:

1. drop the query string and the fragment, then normalise with ``normalize_url``
   (percent-encoding, dot segments, default ports, host case);
2. key on ``(netloc, path)`` -- the SCHEME is ignored, so ``http://`` and ``https://`` rows
   of the same page meet;
3. ``pages.canonical_url`` first, then ``known_urls.page_id`` for keys the canonical URLs do
   not already cover;
4. a key that maps to two different pages is AMBIGUOUS: :meth:`resolve` returns ``None`` and
   the provider counts ``ambiguous_url`` instead of ``unresolved_url``;
5. up to :data:`~ken_contracts.analytics.MAX_UNRESOLVED_SAMPLE` unresolved PATHS (no query
   string, no host) are kept by the provider for the ledger -- :meth:`sample_path` is what
   makes them safe to store.

It satisfies the frozen :class:`~ken_contracts.analytics.PageUrlResolver` and the provider
side's :class:`~ken_core.providers.analytics.base.UrlDiagnostics`. Lookups are pure and
synchronous: the index is in memory for the whole import.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Final
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncSession

from ken_contracts.analytics import MAX_SAMPLE_PATH_CHARS
from ken_contracts.common import PageId
from ken_core.db.repositories import analytics as repo
from ken_services.inventory.urls import try_normalize_url

__all__ = ["InventoryUrlResolver", "url_key"]

_UrlKey = tuple[str, str]
_FORBIDDEN_IN_PATH: Final = frozenset({"?", "#"})


def url_key(url: str) -> _UrlKey | None:
    """``(netloc, path)`` of ``url`` with query and fragment dropped, or ``None``."""
    normalised = try_normalize_url(url)
    if normalised is None:
        return None
    parts = urlsplit(normalised)
    if not parts.netloc:
        return None
    return (parts.netloc.lower(), parts.path or "/")


class InventoryUrlResolver:
    """The inventory's URLs as an in-memory index (see the module docstring)."""

    def __init__(
        self,
        *,
        canonical: Iterable[tuple[str, uuid.UUID]] = (),
        known: Iterable[tuple[str, uuid.UUID]] = (),
    ) -> None:
        self._index: dict[_UrlKey, uuid.UUID] = {}
        self._ambiguous: set[_UrlKey] = set()
        for url, page_id in canonical:
            self._add(url, page_id)
        canonical_keys = set(self._index) | set(self._ambiguous)
        for url, page_id in known:
            key = url_key(url)
            if key is None or key in canonical_keys:
                continue  # a canonical URL is authoritative for its key
            self._add(url, page_id)

    def _add(self, url: str, page_id: uuid.UUID) -> None:
        key = url_key(url)
        if key is None:
            return
        existing = self._index.get(key)
        if existing is None:
            self._index[key] = page_id
        elif existing != page_id:
            self._ambiguous.add(key)

    @classmethod
    async def build(cls, session: AsyncSession) -> InventoryUrlResolver:
        """Read the whole index in two queries (once per import)."""
        canonical, known = await repo.url_index_rows(session)
        return cls(canonical=canonical, known=known)

    # ------------------------------------------------------------- lookups
    def resolve(self, url: str) -> PageId | None:
        key = url_key(url)
        if key is None or key in self._ambiguous:
            return None
        found = self._index.get(key)
        return None if found is None else str(found)

    def ambiguous(self, url: str) -> bool:
        key = url_key(url)
        return key is not None and key in self._ambiguous

    def sample_path(self, url: str) -> str | None:
        """The bounded, query-free PATH of an unresolved URL, safe to store in the ledger."""
        key = url_key(url)
        if key is None:
            return None
        path = key[1]
        if (
            not path.startswith("/")
            or len(path) > MAX_SAMPLE_PATH_CHARS
            or any(char in path for char in _FORBIDDEN_IN_PATH)
            or any(char.isspace() for char in path)
        ):
            return None
        return path

    def __len__(self) -> int:
        return len(self._index)
