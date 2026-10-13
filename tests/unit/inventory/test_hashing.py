"""Content hash (ken_services.inventory.hashing)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ken_contracts.inventory import (
    CONTENT_HASH_VERSION,
    ExtractedLink,
    Heading,
    LinkType,
    PublicationState,
)
from ken_services.inventory.hashing import content_hash, sha256_hex


def _link(ordinal: int, **overrides: Any) -> ExtractedLink:
    values: dict[str, Any] = {
        "ordinal": ordinal,
        "href": "/a",
        "resolved_url": "https://example.com/a",
        "link_type": LinkType.INTERNAL,
        "anchor_text": "anchor",
        "anchor_source": "text",
        "rel": (),
        "block_id": "b0",
        "block_kind": "p",
        "occurrence_index": 0,
        "context_text": "ctx",
        "context_hash": "0" * 64,
    }
    values.update(overrides)
    return ExtractedLink(**values)


def _fields(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "title": "Title",
        "canonical_url": "https://example.com/",
        "meta_description": "Desc",
        "language": "en",
        "publication_state": PublicationState.PUBLISHED,
        "robots_directives": ("noindex", "follow"),
        "headings": (Heading(level=1, text="H", block_id="b0"),),
        "body_text": "Body",
        "links": (_link(0),),
    }
    values.update(overrides)
    return values


def test_sha256_hex() -> None:
    assert sha256_hex("") == hashlib.sha256(b"").hexdigest()
    assert sha256_hex("é") == hashlib.sha256("é".encode()).hexdigest()


def test_exact_serialisation() -> None:
    expected_payload = {
        "hash_version": CONTENT_HASH_VERSION,
        "title": "Title",
        "canonical_url": "https://example.com/",
        "meta_description": "Desc",
        "language": "en",
        "publication_state": "published",
        "robots_directives": ["follow", "noindex"],
        "headings": [[1, "H"]],
        "body_text": "Body",
        "links": [["https://example.com/a", "anchor", [], "internal"]],
    }
    encoded = json.dumps(
        expected_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    assert content_hash(**_fields()) == hashlib.sha256(encoded).hexdigest()


def test_hash_is_hex_sha256_and_stable() -> None:
    first = content_hash(**_fields())
    assert len(first) == 64
    assert first == content_hash(**_fields())
    assert first == first.lower()


def test_robots_order_does_not_matter() -> None:
    assert content_hash(**_fields(robots_directives=("follow", "noindex"))) == content_hash(
        **_fields()
    )


def test_link_rel_order_does_not_matter() -> None:
    a = _fields(links=(_link(0, rel=("nofollow", "ugc")),))
    b = _fields(links=(_link(0, rel=("ugc", "nofollow")),))
    assert content_hash(**a) == content_hash(**b)


def test_links_hashed_in_ordinal_order() -> None:
    one, two = _link(0, href="/1", resolved_url=None), _link(1, href="/2", resolved_url=None)
    assert content_hash(**_fields(links=(two, one))) == content_hash(**_fields(links=(one, two)))


def test_block_and_context_fields_are_excluded() -> None:
    a = _fields(links=(_link(0),))
    b = _fields(
        links=(
            _link(
                0,
                block_id="b9",
                block_kind="li",
                occurrence_index=3,
                context_text="other",
                context_hash="f" * 64,
                anchor_source="img_alt",
                href="./a",
            ),
        ),
        headings=(Heading(level=1, text="H", block_id="b7"),),
    )
    assert content_hash(**a) == content_hash(**b)


def test_href_used_when_unresolved() -> None:
    a = _fields(
        links=(_link(0, href="mailto:a@x", resolved_url=None, link_type=LinkType.NON_HTTP),)
    )
    b = _fields(
        links=(_link(0, href="mailto:b@x", resolved_url=None, link_type=LinkType.NON_HTTP),)
    )
    assert content_hash(**a) != content_hash(**b)


def test_every_content_field_matters() -> None:
    base = content_hash(**_fields())
    changes: list[dict[str, Any]] = [
        {"title": None},
        {"title": "Other"},
        {"canonical_url": "https://example.com/x"},
        {"meta_description": None},
        {"language": "fr"},
        {"publication_state": PublicationState.DRAFT},
        {"robots_directives": ()},
        {"headings": (Heading(level=2, text="H", block_id="b0"),)},
        {"headings": (Heading(level=1, text="H2", block_id="b0"),)},
        {"body_text": "Body!"},
        {"links": ()},
        {"links": (_link(0, resolved_url="https://example.com/b"),)},
        {"links": (_link(0, anchor_text="other"),)},
        {"links": (_link(0, rel=("nofollow",)),)},
        {"links": (_link(0, link_type=LinkType.EXTERNAL),)},
    ]
    for change in changes:
        assert content_hash(**_fields(**change)) != base, change


def test_non_ascii_is_not_escaped_and_is_stable() -> None:
    a = content_hash(**_fields(title="Café — Überblick"))
    assert a == content_hash(**_fields(title="Café — Überblick"))
    assert a != content_hash(**_fields(title="Cafe — Uberblick"))
