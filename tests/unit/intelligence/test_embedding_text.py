"""The embedded text recipe and its input hash."""

from __future__ import annotations

import unicodedata

from ken_services.intelligence.embedding_text import embedding_text, input_hash

PREFIX = "search_document: "


def _text(**overrides: object) -> str:
    fields: dict[str, object] = {
        "title": "EV Market",
        "meta_description": "About EVs",
        "headings": ["Intro", "Outlook"],
        "body_text": "Body one.\n\nBody two.",
        "max_chars": 6000,
        "prefix": PREFIX,
    }
    fields.update(overrides)
    return embedding_text(**fields)  # type: ignore[arg-type]


def test_recipe_layout() -> None:
    assert _text() == (
        "search_document: EV Market\nAbout EVs\nIntro · Outlook\nBody one. Body two."
    )


def test_whitespace_and_unicode_normalised() -> None:
    out = _text(
        title="  EV \t Market ",
        meta_description=unicodedata.normalize("NFD", "Café"),
        headings=["  A  ", "", "B"],
    )
    assert out.startswith("search_document: EV Market\nCafé\nA · B\n")
    assert unicodedata.is_normalized("NFC", out)


def test_empty_parts_dropped() -> None:
    assert _text(title=None, meta_description="", headings=[], body_text="x") == PREFIX + "x"
    assert _text(title=None, meta_description=None, headings=[], body_text="") == PREFIX
    assert _text(prefix="") == "EV Market\nAbout EVs\nIntro · Outlook\nBody one. Body two."


def test_truncation_counts_prefix_and_respects_words() -> None:
    body = " ".join(f"word{n}" for n in range(500))
    out = _text(title=None, meta_description=None, headings=[], body_text=body, max_chars=200)
    assert len(out) <= 200
    assert out.startswith(PREFIX)
    content = out[len(PREFIX) :]
    assert body.startswith(content)
    assert body[len(content)] == " "  # cut at a word boundary
    assert not out.endswith(" ")


def test_truncation_edge_cases() -> None:
    assert _text(max_chars=10, prefix="0123456789abc") == "0123456789"
    hard = _text(title=None, meta_description=None, headings=[], body_text="x" * 50, max_chars=20)
    assert hard == PREFIX + "xxx"
    exact = _text(title="abc", meta_description=None, headings=[], body_text="", max_chars=20)
    assert exact == PREFIX + "abc"


def test_deterministic_and_hash() -> None:
    assert _text() == _text()
    assert input_hash("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert input_hash(_text()) == input_hash(_text())
    assert input_hash("é") != input_hash(unicodedata.normalize("NFD", "é"))
