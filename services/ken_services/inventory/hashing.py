"""Content hash of a normalised page (ADR-0009 section 4).

A new immutable page version is written only when this hash changes, so it
must cover exactly the page's CONTENT and nothing about how it was fetched.

Included (``hash_version`` 1):

* ``hash_version`` itself, so a future algorithm can never collide with this one;
* title, canonical URL, meta description, language, publication state;
* robots directives (sorted);
* headings as ``[level, text]`` in document order;
* body text;
* every body link as ``[resolved_url or href, anchor_text, sorted rel, link_type]``
  in ordinal order. A changed link target with unchanged text is a content change.

Excluded: fetch time, HTTP status, redirect chain, requested URL, block ids,
page type, published/modified timestamps, metadata, ``body_html``, external
and source ids. None of them is the page's content, and several (block ids,
``body_html``) change with markup formatting alone.

Serialisation is canonical JSON (sorted keys, no insignificant whitespace,
UTF-8), so the hash is byte-stable across processes and platforms.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from ken_contracts.inventory import (
    CONTENT_HASH_VERSION,
    ExtractedLink,
    Heading,
    NormalizedPage,
    PublicationState,
)

__all__ = ["content_hash", "page_content_hash", "sha256_hex"]


def sha256_hex(text: str) -> str:
    """Lower-case hex SHA-256 of ``text`` encoded as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_hash(
    *,
    title: str | None,
    canonical_url: str,
    meta_description: str | None,
    language: str | None,
    publication_state: PublicationState,
    robots_directives: Iterable[str],
    headings: Iterable[Heading],
    body_text: str,
    links: Iterable[ExtractedLink],
) -> str:
    """SHA-256 hex digest over the content fields of a :class:`NormalizedPage`.

    Keyword-only so the normaliser can call it last, once every field is final,
    without any risk of positional mix-ups.
    """
    ordered_links = sorted(links, key=lambda link: link.ordinal)
    payload: dict[str, Any] = {
        "hash_version": CONTENT_HASH_VERSION,
        "title": title,
        "canonical_url": canonical_url,
        "meta_description": meta_description,
        "language": language,
        "publication_state": publication_state.value,
        "robots_directives": sorted(robots_directives),
        "headings": [[heading.level, heading.text] for heading in headings],
        "body_text": body_text,
        "links": [
            [
                link.resolved_url if link.resolved_url is not None else link.href,
                link.anchor_text,
                sorted(link.rel),
                link.link_type.value,
            ]
            for link in ordered_links
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def page_content_hash(page: NormalizedPage) -> str:
    """Recompute the content hash of an already-normalised page (verification aid)."""
    return content_hash(
        title=page.title,
        canonical_url=page.canonical_url,
        meta_description=page.meta_description,
        language=page.language,
        publication_state=page.publication_state,
        robots_directives=page.robots_directives,
        headings=page.headings,
        body_text=page.body_text,
        links=page.links,
    )
