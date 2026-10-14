"""The embedding recipe fingerprint (ADR-0010 section 2) and the relationship cap setting."""

from __future__ import annotations

import hashlib
import re

import pytest
from pydantic import ValidationError

from ken_contracts.intelligence import EMBEDDING_RECIPE_VERSION
from ken_services.intelligence.embeddings import current_recipe_fingerprint, recipe_fingerprint
from tests.conftest import make_settings


def test_fingerprint_is_the_documented_hash() -> None:
    material = f"{EMBEDDING_RECIPE_VERSION}|search_document: |6000".encode()
    expected = f"r{EMBEDDING_RECIPE_VERSION}-" + hashlib.sha256(material).hexdigest()[:16]
    assert recipe_fingerprint(document_prefix="search_document: ", max_chars=6000) == expected
    assert re.fullmatch(r"r\d+-[0-9a-f]{16}", expected)


def test_fingerprint_is_deterministic_and_sensitive_to_every_input() -> None:
    base = recipe_fingerprint(document_prefix="p: ", max_chars=6000)
    assert base == recipe_fingerprint(document_prefix="p: ", max_chars=6000)
    assert base != recipe_fingerprint(document_prefix="p:", max_chars=6000)
    assert base != recipe_fingerprint(document_prefix="", max_chars=6000)
    assert base != recipe_fingerprint(document_prefix="p: ", max_chars=6001)


def test_current_fingerprint_follows_the_settings() -> None:
    default = make_settings()
    assert current_recipe_fingerprint(default) == recipe_fingerprint(
        document_prefix=default.embedding_document_prefix,
        max_chars=default.embedding_max_chars,
    )
    assert current_recipe_fingerprint(default) != current_recipe_fingerprint(
        make_settings(embedding_document_prefix="")
    )
    assert current_recipe_fingerprint(default) != current_recipe_fingerprint(
        make_settings(embedding_max_chars=default.embedding_max_chars + 1)
    )
    # Settings outside the recipe text do not change it.
    assert current_recipe_fingerprint(default) == current_recipe_fingerprint(
        make_settings(embedding_query_prefix="q: ", embedding_batch_size=3)
    )


def test_relationship_cap_setting() -> None:
    assert make_settings().relationship_max_per_type == 25
    assert make_settings(relationship_max_per_type=1).relationship_max_per_type == 1
    assert make_settings(relationship_max_per_type=500).relationship_max_per_type == 500
    for bad in (0, 501):
        with pytest.raises(ValidationError):
            make_settings(relationship_max_per_type=bad)
