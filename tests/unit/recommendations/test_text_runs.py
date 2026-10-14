"""``ContentBlock.runs`` / ``enclosing`` (ADR-0011 section 5): parity and exact ancestors."""

from __future__ import annotations

import pytest

from ken_services.inventory.normalize import ContentBlock, TextRun, extract_blocks
from tests.unit.recommendations._block_corpus import block_digest, corpus
from tests.unit.recommendations._support import blocks

# Computed with the normaliser BEFORE text runs existed (see _block_corpus.py).
BASELINE: tuple[int, int, str] = (
    97,
    937,
    "040dbf31fa2892c9dcf6b13017bcffa0d846d859ded2e5c20fdb8e327ef16a26",
)


def _texts(block: ContentBlock) -> list[tuple[str, frozenset[str]]]:
    return [(block.text[r.start : r.end], r.ancestors) for r in block.runs]


def _reassemble(block: ContentBlock) -> str:
    out, cursor = [], 0
    for run in block.runs:
        out.append(block.text[cursor : run.start])  # gap: walk separators
        out.append(block.text[run.start : run.end])
        cursor = run.end
    out.append(block.text[cursor:])
    return "".join(out)


# --------------------------------------------------------------------------- #
# (ii) block ids, kinds and text unchanged over every corpus
# --------------------------------------------------------------------------- #
def test_block_ids_kinds_and_text_unchanged_over_all_corpora() -> None:
    pages = corpus()
    labels = {label.split(":", 1)[0] for label, _ in pages}
    assert labels == {"site", "export", "demo"}
    assert block_digest(pages) == BASELINE


def test_corpus_runs_are_ordered_maximal_and_gaps_are_separators() -> None:
    checked = 0
    for _label, page in corpus():
        for block in extract_blocks(page.body_html):
            # (i) runs + gaps reproduce the text; gaps are walk separators (spaces) only.
            assert _reassemble(block) == block.text
            cursor = 0
            for run in block.runs:
                assert cursor <= run.start < run.end <= len(block.text)
                assert block.text[cursor : run.start].strip() == ""
                cursor = run.end
            assert block.text[cursor:].strip() == ""
            for left, right in zip(block.runs, block.runs[1:], strict=False):
                if left.end == right.start:  # touching runs must differ (maximal)
                    assert left.ancestors != right.ancestors
            checked += 1
    assert checked > 900


def test_every_corpus_link_text_sits_in_a_run_under_an_anchor() -> None:
    seen = 0
    for _label, page in corpus():
        by_id = {b.block_id: b for b in extract_blocks(page.body_html)}
        for link in page.links:
            if link.anchor_source != "text" or not link.anchor_text:
                continue
            block = by_id[link.block_id]
            if link.anchor_text not in block.text:
                continue  # the link's text lives in a nested block (<a><p>..</p></a>)
            under_a = "".join(
                block.text[run.start : run.end] for run in block.runs if "a" in run.ancestors
            )
            # Every word of the anchor is text under an <a> in this block.
            assert all(word in under_a for word in link.anchor_text.split()), link
            seen += 1
    assert seen > 50


def test_normalize_document_output_is_unaffected() -> None:
    # body_text is still the join of the block texts (the Phase 1 identity).
    for _label, page in corpus():
        joined = "\n\n".join(b.text for b in extract_blocks(page.body_html) if b.text)
        assert joined == page.body_text


# --------------------------------------------------------------------------- #
# (iii) exact ancestors
# --------------------------------------------------------------------------- #
def test_td_p_is_enclosed_by_the_table() -> None:
    (block,) = blocks("<table><tr><td><p>in a cell</p></td></tr></table>")
    assert block.kind == "p"
    assert _texts(block) == [("in a cell", frozenset())]
    assert {"td", "tr", "table"} <= block.enclosing


def test_blockquote_p_is_enclosed_by_the_quote() -> None:
    (block,) = blocks("<blockquote><p>quoted words</p></blockquote>")
    assert block.kind == "p"
    assert block.enclosing == frozenset({"blockquote"})
    assert _texts(block) == [("quoted words", frozenset())]


def test_inline_link_is_its_own_run() -> None:
    (block,) = blocks('<p>see <a href="/x">x</a></p>')
    assert block.text == "see x"
    assert block.runs == (
        TextRun(start=0, end=4, ancestors=frozenset()),
        TextRun(start=4, end=5, ancestors=frozenset({"a"})),
    )
    assert block.enclosing == frozenset()


def test_code_is_its_own_run() -> None:
    (block,) = blocks("<p><code>run()</code> then</p>")
    assert _texts(block) == [("run()", frozenset({"code"})), (" then", frozenset())]


def test_nested_inline_ancestors_accumulate() -> None:
    (block,) = blocks('<p>a <a href="/x"><b>bold link</b></a> z</p>')
    assert _texts(block) == [
        ("a ", frozenset()),
        ("bold link", frozenset({"a", "b"})),
        (" z", frozenset()),
    ]


def test_list_item_is_enclosed_by_its_list() -> None:
    first, second = blocks("<ul><li>one</li><li>two <em>x</em></li></ul>")
    assert first.enclosing == second.enclosing == frozenset({"ul"})
    assert _texts(second) == [("two ", frozenset()), ("x", frozenset({"em"}))]


def test_walk_separators_belong_to_no_run() -> None:
    (block,) = blocks("<p>alpha<br>beta</p>")
    assert block.text == "alpha beta"
    assert _texts(block) == [("alpha", frozenset()), ("beta", frozenset())]


def test_collapsed_whitespace_keeps_a_shared_label_and_drops_a_mixed_one() -> None:
    (block,) = blocks("<p>a   b  <i>c </i> d</p>")
    assert block.text == "a b c d"
    # "a b " share the plain label; the space between </i> and d mixes {i} and plain.
    assert _texts(block) == [("a b ", frozenset()), ("c", frozenset({"i"})), ("d", frozenset())]


def test_nfc_across_a_label_boundary_belongs_to_no_run() -> None:
    (block,) = blocks("<p>cafe<b>́</b> ok</p>")
    assert block.text == "café ok"
    assert _texts(block) == [(" ok", frozenset())]


def test_nfc_inside_one_label_is_kept() -> None:
    (block,) = blocks("<p>caf<b>é</b> ok</p>")
    assert _texts(block) == [("caf", frozenset()), ("é", frozenset({"b"})), (" ok", frozenset())]


def test_implicit_container_block_and_link_only_block() -> None:
    div, link_only = blocks('<div>loose <span>text</span></div><p><a href="/z"></a></p>')
    assert div.kind == "div"
    assert _texts(div) == [("loose ", frozenset()), ("text", frozenset({"span"}))]
    assert link_only.text == ""
    assert link_only.runs == ()


@pytest.mark.parametrize(
    "html",
    [
        "<p>unclosed <b>bold <i>italic</p><div>after</div>",
        "<main><a href='/a'><a href='/b'>nested anchors</a></a></main>",
        "<p>\x00 nul</p>",
        "<pre>  code\n  block </pre>",
        "<p>Caf&eacute; &amp; &lt;tags&gt; <a href='/e'>&quot;q&quot;</a></p>",
    ],
)
def test_tricky_markup_runs_reassemble(html: str) -> None:
    for block in extract_blocks(f"<body>{html}</body>"):
        assert _reassemble(block) == block.text


def test_equality_still_ignores_derived_fields() -> None:
    (block,) = blocks("<p>Para <a href='/x'>x</a>.</p>")
    assert block == ContentBlock(block_id="b0", kind="p", text="Para x.")
    assert block.runs  # but the runs are there
