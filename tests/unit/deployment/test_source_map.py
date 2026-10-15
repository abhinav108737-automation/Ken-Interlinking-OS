"""``source_map``: exact source positions for the inventory block walk."""

from __future__ import annotations

import unicodedata

import pytest

from ken_services.deployment.source_map import (
    map_document,
    nfc_boundary,
    script_double_escapes,
    style_hides,
)
from ken_services.inventory.normalize import extract_blocks
from tests.unit.deployment._support import demo_fragments, demo_pages, full_document


@pytest.mark.parametrize("which", ["stored", "export", "full"])
def test_every_text_node_of_the_demo_corpus_maps_to_its_source(which: str) -> None:
    nodes = 0
    for page in demo_pages():
        fragment = demo_fragments()[page.url]
        html = {"stored": page.body_html, "export": fragment, "full": full_document(fragment)}[
            which
        ]
        doc = map_document(html)
        assert doc.sound
        for block in doc.blocks:
            for node in block.nodes:
                if node is None:
                    continue
                source = doc.text_sources[id(node)]
                if not str(node).strip():
                    continue  # bs4 collapses whitespace-only strings: never anchor hosts
                assert source.reliable and source.contiguous
                assert (
                    html[source.src_start : source.src_end] == str(node)
                    or "&" in html[source.src_start : source.src_end]
                )
                nodes += 1
    assert nodes > 500


def test_walk_replay_equals_extract_blocks_including_runs() -> None:
    html = (
        "<main><div>Intro <p>A <b>bold</b> move</p> outro<ul><li>x<li>y</ul></div>"
        "<table><tr><td><p>cell</p></td></tr></table><a href=x></a></main>"
    )
    doc = map_document(html)
    assert doc.sound
    assert [m.block for m in doc.blocks] == list(extract_blocks(html))
    assert [m.block.runs for m in doc.blocks] == [b.runs for b in extract_blocks(html)]


def test_references_are_atomic_pieces() -> None:
    html = "<p>a&amp;b &#169; &#xA9; &copy &unknown; c</p>"
    doc = map_document(html)
    node = doc.blocks[0].nodes[0]
    assert node is not None
    source = doc.text_sources[id(node)]
    assert source.reliable and source.contiguous
    references = [html[p.src_start : p.src_end] for p in source.pieces if not p.verbatim]
    assert references == ["&amp;", "&#169;", "&#xA9;", "&copy", "&unknown;"]
    position = str(node).index("b")
    assert source.to_source(position, start=True) == html.index("b ")
    inside = next(p for p in source.pieces if not p.verbatim and p.dec_end - p.dec_start > 1)
    assert source.to_source(inside.dec_start + 1, start=True) is None  # "&unknown;" literal


@pytest.mark.parametrize(
    ("raw", "offset", "expected"),
    [
        ("plain ascii", 5, 5),
        ("cafe\u0301 au lait", 4, 5),  # after the composed "e + acute"
        ("cafe\u0301 au lait", 3, 3),
        ("cafe\u0301 au lait", 5, 6),
        ("\u1100\u1161 hangul", 1, 2),  # L+V jamo compose to one syllable
        ("a\u0323\u0302b", 1, 3),  # reordered and composed marks
    ],
)
def test_nfc_boundary_maps_safe_cuts(raw: str, offset: int, expected: int) -> None:
    assert nfc_boundary(raw, offset) == expected
    normal = unicodedata.normalize("NFC", raw)
    cut = nfc_boundary(raw, offset)
    assert cut is not None
    assert unicodedata.normalize("NFC", raw[:cut]) == normal[:offset]
    assert unicodedata.normalize("NFC", raw[cut:]) == normal[offset:]


def test_nfc_boundary_refuses_cuts_inside_a_composed_character() -> None:
    # NFC("x\u0344y") is "\u1e8d\u0301y": U+0344 expands to two marks and the first
    # composes with "x", so offset 1 falls inside the one source character U+0344.
    assert nfc_boundary("x\u0344y", 1) is None
    assert nfc_boundary("x\u0344y", 2) == 2


@pytest.mark.parametrize(
    ("style", "hidden"),
    [
        ("display:none", True),
        (" Display : None ; ", True),
        ("display:none!important", True),
        ("display: none ! important", True),
        ("color:red;/* hi */visibility:hidden", True),
        ("visibility: collapse", True),
        ("content-visibility:hidden", True),
        ("display:block", False),
        ("display:inline-none", False),
        ("visibility:visible", False),
        ("font-display:none", False),
        ("", False),
        (None, False),
        ("garbage;;;:", False),
    ],
)
def test_style_hides(style: str | None, hidden: bool) -> None:
    assert style_hides(style) is hidden


def test_link_facts_include_ancestors_above_the_root() -> None:
    html = '<div style="display:none"><main><p>x <a href="/a">y</a></p></main></div>'
    doc = map_document(html)
    (link,) = doc.links
    assert link.hidden and link.in_root and not link.nested


def test_unclosed_links_are_recorded() -> None:
    html = '<p><a href="/a">open</p><p><a href="/b">closed</a> <a name="x"/>self</p>'
    doc = map_document(html)
    assert doc.unclosed_link_starts == (html.index('<a href="/a"'), html.index('<a name="x"'))


def test_parser_failure_is_unsound_not_an_exception() -> None:
    for html in ("<!DOCTYPE", "<![CDATA[", "</", "<?pi", "&#", "<a b='"):
        doc = map_document(html)
        assert isinstance(doc.sound, bool)


@pytest.mark.parametrize(
    ("script", "sound"),
    [
        ('var a="<!--<script>";', False),
        ('var a="<!-- x <SCRIPT>";', False),
        ('var a="<!--";', True),
        ('var s="<script>"; var c="<!--";', True),
        ("", True),
    ],
)
def test_a_double_escaping_script_makes_the_document_unsound(script: str, sound: bool) -> None:
    html = f"<main><script>{script}</script><p>EV charging market</p></main>"
    assert map_document(html).sound is sound
    assert script_double_escapes(script) is not sound
