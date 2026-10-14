"""The deterministic text recipe embedded per page version (ADR-0010 section 2).

Recipe ``EMBEDDING_RECIPE_VERSION = 1``::

    prefix + "\\n".join(non-empty parts of [title, meta_description,
                                            " · ".join(headings), body_text])

Each part is NFC-normalised with every whitespace run collapsed to one space;
empty parts are dropped (no blank lines). The whole string, prefix included, is
cut to ``max_chars`` at a word boundary. ``prefix`` is configuration (e.g.
nomic-embed-text's ``"search_document: "``) and is used verbatim. Changing any
of this requires bumping ``EMBEDDING_RECIPE_VERSION``.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Sequence
from typing import Final

from ken_services.intelligence.text import truncate_words

__all__ = ["HEADING_SEPARATOR", "embedding_text", "input_hash"]

HEADING_SEPARATOR: Final = " · "


def _norm(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(unicodedata.normalize("NFC", text).split())


def embedding_text(
    *,
    title: str | None,
    meta_description: str | None,
    headings: Sequence[str],
    body_text: str,
    max_chars: int,
    prefix: str,
) -> str:
    """The exact string sent to the embedding model for one page version."""
    heading_line = HEADING_SEPARATOR.join(h for h in (_norm(h) for h in headings) if h)
    parts = [_norm(title), _norm(meta_description), heading_line, _norm(body_text)]
    text = prefix + "\n".join(part for part in parts if part)
    if len(text) <= max_chars:
        return text
    if len(prefix) >= max_chars:
        return prefix[:max_chars]
    content = truncate_words(text[len(prefix) :], max_chars - len(prefix), ellipsis=False)
    return prefix + content


def input_hash(text: str) -> str:
    """SHA-256 hex digest of ``text`` encoded as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
