"""``HtmlMutator`` (ADR-0014 sections 2 and 8): splice-exact insertion, typed refusals, removal."""

from __future__ import annotations

import pytest

from ken_contracts.deployment import SNIPPET_CHARS
from ken_contracts.deployment import DeploymentIssueCode as Code
from ken_services.deployment.mutation import HtmlMutator, change_snippets, link_start_tag
from ken_services.deployment.protocol import LinkInsertion, MutationResult
from ken_services.inventory.hashing import sha256_hex
from ken_services.inventory.normalize import extract_blocks
from tests.unit.deployment._support import (
    EXCLUDED,
    HREF,
    MUTATOR,
    OTHER_HREF,
    codes,
    insertion,
    plan,
    removal,
)


def insert(
    html: str, *items: LinkInsertion, excluded: tuple[str, ...] = EXCLUDED
) -> MutationResult:
    return MUTATOR.insert_links(html, list(items), excluded_ancestors=excluded)


def wrapped(html: str, source_anchor: str, item: LinkInsertion, *, nth: int = 0) -> str:
    """``html`` with the ``nth`` occurrence of ``source_anchor`` wrapped in our link."""
    position = -1
    for _ in range(nth + 1):
        position = html.index(source_anchor, position + 1)
    end = position + len(source_anchor)
    tag = link_start_tag(item.href, item.plan_id)
    return f"{html[:position]}{tag}{source_anchor}</a>{html[end:]}"


def assert_refused(html: str, result: object, *expected: Code) -> None:
    assert codes(result) == [code.value for code in expected]
    assert getattr(result, "html") == html  # noqa: B009
    assert getattr(result, "applied") == ()  # noqa: B009


def assert_round_trip(html: str, *items: LinkInsertion) -> str:
    result = insert(html, *items)
    assert result.ok, codes(result)
    assert result.applied == tuple(item.plan_id for item in items)
    undone = MUTATOR.remove_links(result.html, [removal(item) for item in items])
    assert undone.ok and undone.html == html
    return result.html


PARA = (
    "<main><article><h1>India EV Charging</h1>"
    "<p>Demand for battery storage is rising across India, and battery storage "
    "projects need charging infrastructure.</p>"
    "<p>Rooftop solar and grid-scale storage are covered elsewhere.</p></article></main>"
)


# --------------------------------------------------------------------------- #
# Splicing
# --------------------------------------------------------------------------- #
def test_single_insertion_is_an_exact_splice() -> None:
    item = insertion(PARA, "battery storage")
    result = insert(PARA, item)
    assert result.ok and result.applied == (item.plan_id,)
    assert result.html == wrapped(PARA, "battery storage", item)
    assert [b.text for b in extract_blocks(result.html)] == [b.text for b in extract_blocks(PARA)]


def test_second_occurrence_wraps_the_second_match() -> None:
    item = insertion(PARA, "battery storage", occurrence=1)
    result = insert(PARA, item)
    assert result.html == wrapped(PARA, "battery storage", item, nth=1)


def test_multiple_insertions_in_different_blocks() -> None:
    first = insertion(PARA, "charging infrastructure", plan_id=1)
    second = insertion(PARA, "rooftop solar", plan_id=2, href=OTHER_HREF)
    out = assert_round_trip(PARA, first, second)
    expected = wrapped(wrapped(PARA, "charging infrastructure", first), "Rooftop solar", second)
    assert out == expected


def test_multiple_insertions_in_one_block() -> None:
    first = insertion(PARA, "battery storage", plan_id=1)
    second = insertion(PARA, "charging infrastructure", plan_id=2, href=OTHER_HREF)
    assert_round_trip(PARA, first, second)


def test_adjacent_spans_separated_by_punctuation() -> None:
    html = "<main><p>We track battery storage/solar power deployments.</p></main>"
    first = insertion(html, "battery storage", plan_id=1)
    second = insertion(html, "solar power", plan_id=2, href=OTHER_HREF)
    out = assert_round_trip(html, first, second)
    assert "</a>/<a " in out


def test_overlapping_spans_are_refused_all_or_nothing() -> None:
    html = "<main><p>Battery energy storage systems are growing.</p></main>"
    first = insertion(html, "battery energy storage", plan_id=1)
    second = insertion(html, "energy storage systems", plan_id=2)
    assert_refused(
        html, insert(html, first, second), Code.OVERLAPPING_CHANGES, Code.OVERLAPPING_CHANGES
    )


def test_duplicate_occurrence_index_is_overlapping() -> None:
    first = insertion(PARA, "battery storage", plan_id=1)
    second = insertion(PARA, "battery storage", plan_id=2, href=OTHER_HREF)
    result = insert(PARA, first, second)
    assert_refused(PARA, result, Code.OVERLAPPING_CHANGES, Code.OVERLAPPING_CHANGES)


def test_duplicate_plan_id_in_one_batch_is_refused() -> None:
    first = insertion(PARA, "battery storage", plan_id=7)
    second = insertion(PARA, "rooftop solar", plan_id=7)
    assert_refused(
        PARA, insert(PARA, first, second), Code.OVERLAPPING_CHANGES, Code.OVERLAPPING_CHANGES
    )


def test_empty_batch_changes_nothing() -> None:
    result = insert(PARA)
    assert result.ok and result.html == PARA and result.applied == ()


# --------------------------------------------------------------------------- #
# Unicode
# --------------------------------------------------------------------------- #
def test_precomposed_unicode_anchor() -> None:
    html = "<main><p>Le march\xe9 de l'\xe9nergie solaire cro\xeet vite.</p></main>"
    item = insertion(html, "\xe9nergie solaire")
    assert insert(html, item).html == wrapped(html, "\xe9nergie solaire", item)
    assert_round_trip(html, item)


def test_decomposed_source_is_preserved_byte_for_byte() -> None:
    decomposed = "e\u0301nergie solaire"  # NFD in the source; the block text is NFC
    html = f"<main><p>Le march\xe9 de l'{decomposed} cro\xeet vite.</p></main>"
    item = insertion(html, "\xe9nergie solaire")
    assert item.anchor_text == "\xe9nergie solaire"
    result = insert(html, item)
    assert result.ok
    assert result.html == wrapped(html, decomposed, item)
    assert_round_trip(html, item)


def test_anchor_ending_with_a_combining_mark() -> None:
    html = "<main><p>The best cafe\u0301 society in town.</p></main>"
    item = insertion(html, "best caf\xe9")
    assert insert(html, item).html == wrapped(html, "best cafe\u0301", item)


def test_emoji_and_astral_characters_around_and_inside_the_anchor() -> None:
    html = (
        "<main><p>\U0001f50b battery \U0001f50b storage and battery storage \U0001f50b</p></main>"
    )
    item = insertion(html, "battery storage")
    assert insert(html, item).html == wrapped(html, "battery storage", item)
    weird = insertion(html, "battery \U0001f50b storage", plan_id=2)
    assert_round_trip(html, weird)


def test_devanagari_with_combining_vowel_signs() -> None:
    html = (
        "<main><p>\u092d\u093e\u0930\u0924 \u092e\u0947\u0902 "
        "\u092c\u0948\u091f\u0930\u0940 \u092d\u0902\u0921\u093e\u0930\u0923 "
        "\u092c\u0922\u093c \u0930\u0939\u093e \u0939\u0948</p></main>"
    )
    item = insertion(html, "\u092c\u0948\u091f\u0930\u0940 \u092d\u0902\u0921\u093e\u0930\u0923")
    assert_round_trip(html, item)


def test_nfc_composing_across_two_text_nodes_is_unmappable() -> None:
    # NFC composes "e" (in <b>) with the combining accent after it: not part-local.
    html = "<main><p>Caf<b>e</b>\u0301 owners and battery storage buyers.</p></main>"
    item = insertion(html, "battery storage")
    assert_refused(html, insert(html, item), Code.UNMAPPABLE_SPAN)


# --------------------------------------------------------------------------- #
# Character and entity references
# --------------------------------------------------------------------------- #
def test_entity_inside_the_anchor_is_preserved_verbatim() -> None:
    html = "<main><p>New R&amp;D centres open in Pune.</p></main>"
    item = insertion(html, "R&D centres")
    assert insert(html, item).html == wrapped(html, "R&amp;D centres", item)


def test_entity_at_the_start_boundary() -> None:
    html = "<main><p>Le march\xe9 de l'&eacute;nergie solaire cro\xeet.</p></main>"
    item = insertion(html, "\xe9nergie solaire")
    assert insert(html, item).html == wrapped(html, "&eacute;nergie solaire", item)


def test_entity_at_the_end_boundary() -> None:
    html = "<main><p>Visit the best caf&#xE9; today.</p></main>"
    item = insertion(html, "best caf\xe9")
    assert insert(html, item).html == wrapped(html, "best caf&#xE9;", item)


def test_entity_without_semicolon_is_one_atomic_reference() -> None:
    html = "<main><p>Fish &amp chips &amp; battery storage &copy 2026.</p></main>"
    item = insertion(html, "battery storage")
    assert_round_trip(html, item)


def test_nbsp_entity_inside_the_anchor() -> None:
    html = "<main><p>Demand for battery&nbsp;storage is rising.</p></main>"
    item = insertion(html, "battery storage")
    assert item.anchor_text == "battery storage"
    assert insert(html, item).html == wrapped(html, "battery&nbsp;storage", item)


def test_entity_right_after_the_anchor_stays_outside() -> None:
    html = "<main><p>Demand for battery storage&nbsp;is rising.</p></main>"
    item = insertion(html, "battery storage")
    assert insert(html, item).html == wrapped(html, "battery storage", item)


def test_boundary_inside_a_multi_character_entity_is_unmappable() -> None:
    # &NotEqualTilde; decodes to two code points; the anchor ends between them.
    html = "<main><p>The relation x &NotEqualTilde; y holds.</p></main>"
    blocks = extract_blocks(html)
    text = blocks[0].text
    start = text.index("x")
    end = text.index("\u2242") + 1
    item = LinkInsertion(
        plan_id=plan(1),
        href=HREF,
        anchor_text=text[start:end],
        context_hash=sha256_hex(text),
        block_id_hint=blocks[0].block_id,
        occurrence_index=0,
        start=start,
        end=end,
    )
    assert_refused(html, insert(html, item), Code.UNMAPPABLE_SPAN)


# --------------------------------------------------------------------------- #
# Whitespace
# --------------------------------------------------------------------------- #
def test_newline_and_indentation_inside_the_anchor() -> None:
    html = "<main>\n  <p>\n    Demand for battery\n      storage is rising.\n  </p>\n</main>\n"
    item = insertion(html, "battery storage")
    assert item.anchor_text == "battery storage"
    assert insert(html, item).html == wrapped(html, "battery\n      storage", item)


def test_crlf_line_endings() -> None:
    html = "<main>\r\n<p>Demand for battery\r\nstorage is rising.</p>\r\n</main>\r\n"
    item = insertion(html, "battery storage")
    assert insert(html, item).html == wrapped(html, "battery\r\nstorage", item)
    assert_round_trip(html, item)


# --------------------------------------------------------------------------- #
# Text-node structure
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "inner",
    [
        "battery <b>storage</b>",
        "<i>battery</i> storage",
        "battery<br>storage",
        "battery <!-- note -->storage",
        "battery <span>storage</span>",
    ],
)
def test_anchor_across_text_nodes_or_inline_tags_is_refused(inner: str) -> None:
    html = f"<main><p>Demand for {inner} is rising.</p></main>"
    item = insertion(html, "battery storage")
    assert_refused(html, insert(html, item), Code.ANCHOR_NOT_IN_SINGLE_TEXT_NODE)


def test_anchor_wholly_inside_an_inline_element() -> None:
    html = "<main><p>Demand for <em>battery storage</em> is rising.</p></main>"
    item = insertion(html, "battery storage")
    result = insert(html, item)
    assert result.html == wrapped(html, "battery storage", item)
    assert "<em><a " in result.html


def test_inside_an_existing_link_is_unsafe() -> None:
    html = f'<main><p>See <a href="{OTHER_HREF}">battery storage projects</a>.</p></main>'
    item = insertion(html, "battery storage")
    assert_refused(html, insert(html, item), Code.UNSAFE_PLACEMENT)


def test_existing_link_with_another_href_at_the_span_conflicts() -> None:
    html = f'<main><p>See <a href="{OTHER_HREF}">battery storage</a> now.</p></main>'
    item = insertion(html, "battery storage")
    assert_refused(html, insert(html, item), Code.CONFLICTING_LINK)


def test_link_above_the_content_root_is_unsafe() -> None:
    html = (
        f'<body><a href="{OTHER_HREF}"><main><p>Demand for battery storage.</p></main></a></body>'
    )
    item = insertion(html, "battery storage")
    assert_refused(html, insert(html, item), Code.UNSAFE_PLACEMENT)


# --------------------------------------------------------------------------- #
# Excluded ancestors and hidden ancestors
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "markup",
    [
        "<table><tr><td>Demand for battery storage is rising.</td></tr></table>",
        "<table><tr><th>Demand for battery storage is rising.</th></tr></table>",
        "<table><tr><td><p>Demand for battery storage is rising.</p></td></tr></table>",
        "<blockquote><p>Demand for battery storage is rising.</p></blockquote>",
        "<figure><figcaption>Demand for battery storage is rising.</figcaption></figure>",
        "<figure><p>Demand for battery storage is rising.</p></figure>",
        "<p>Run <code>demand for battery storage</code> now.</p>",
        "<pre>Demand for battery storage is rising.</pre>",
        "<h2>Demand for battery storage is rising</h2>",
        "<p><label>Demand for battery storage is rising.</label></p>",
        "<p><small>Demand for battery storage is rising.</small></p>",
        "<details><summary>More</summary><p>Demand for battery storage is rising.</p></details>",
        "<footer><p>Demand for battery storage is rising.</p></footer>",
        "<header><p>Demand for battery storage is rising.</p></header>",
        "<p><cite>Demand for battery storage</cite> is rising.</p>",
        "<p><q>Demand for battery storage</q> is rising.</p>",
        "<p>Demand<sup>for battery storage</sup> is rising.</p>",
        "<p><del>Demand for battery storage is rising.</del></p>",
        "<address>Demand for battery storage is rising.</address>",
        "<dialog open><p>Demand for battery storage is rising.</p></dialog>",
    ],
)
def test_excluded_ancestors_are_unsafe(markup: str) -> None:
    html = f"<main><article>{markup}</article></main>"
    item = insertion(html, "battery storage")
    assert_refused(html, insert(html, item), Code.UNSAFE_PLACEMENT)


def test_a_custom_exclusion_list_is_honoured_and_links_are_always_excluded() -> None:
    html = "<main><p>Demand for <mark>battery storage</mark> is rising.</p></main>"
    item = insertion(html, "battery storage")
    assert insert(html, item, excluded=()).ok
    assert_refused(html, insert(html, item, excluded=("mark",)), Code.UNSAFE_PLACEMENT)
    linked = f'<main><p>See <a href="{OTHER_HREF}">battery storage projects</a>.</p></main>'
    assert_refused(
        linked,
        insert(linked, insertion(linked, "battery storage"), excluded=()),
        Code.UNSAFE_PLACEMENT,
    )


@pytest.mark.parametrize(
    "style",
    [
        "display:none",
        "DISPLAY : NONE",
        "color: red; display: none !important",
        "/* x */ display:/* y */none",
        "visibility:hidden",
        "Visibility: Collapse",
        "content-visibility: hidden",
    ],
)
def test_inline_style_hidden_ancestor_is_unsafe(style: str) -> None:
    html = f'<main><div style="{style}"><p>Demand for battery storage is rising.</p></div></main>'
    item = insertion(html, "battery storage")
    assert_refused(html, insert(html, item), Code.UNSAFE_PLACEMENT)


def test_visible_styles_are_not_hidden() -> None:
    html = (
        '<main><div style="display: block; visibility: visible">'
        "<p>Demand for battery storage.</p></div></main>"
    )
    assert insert(html, insertion(html, "battery storage")).ok


@pytest.mark.parametrize(
    "wrapper",
    [
        "<div hidden>{}</div>",
        '<div aria-hidden="TRUE">{}</div>',
        '<div style="display:none">{}</div>',
    ],
)
def test_hidden_ancestor_above_the_content_root_is_unsafe(wrapper: str) -> None:
    html = "<body>" + wrapper.format("<main><p>Demand for battery storage.</p></main>") + "</body>"
    item = insertion(html, "battery storage")
    assert_refused(html, insert(html, item), Code.UNSAFE_PLACEMENT)


def test_hidden_attribute_inside_the_root_removes_the_block() -> None:
    visible = "<main><p>Demand for battery storage.</p><p>Other text here.</p></main>"
    item = insertion(visible, "battery storage")
    hidden = visible.replace("<p>Demand", "<p hidden>Demand")
    assert_refused(hidden, insert(hidden, item), Code.BLOCK_NOT_FOUND)


# --------------------------------------------------------------------------- #
# Comments, script, style, raw text
# --------------------------------------------------------------------------- #
def test_comments_script_and_style_are_left_untouched() -> None:
    html = (
        "<main><!-- battery storage --><script>var s = '<p>battery storage</p>';</script>"
        "<style>p::after{content:'battery storage'}</style>"
        "<p>Demand <!-- a note --> for battery storage is rising.</p></main>"
    )
    item = insertion(html, "battery storage")
    result = insert(html, item)
    assert result.ok
    assert result.html == wrapped(html, "battery storage", item, nth=3)
    assert_round_trip(html, item)


@pytest.mark.parametrize("element", ["xmp", "noembed", "noframes"])
def test_raw_text_elements_are_unmappable(element: str) -> None:
    html = f"<main><div><{element}>Demand for battery storage.</{element}></div></main>"
    item = insertion(html, "battery storage")
    assert_refused(html, insert(html, item), Code.UNMAPPABLE_SPAN)


def test_plaintext_element_is_unmappable() -> None:
    html = "<main><div><plaintext>Demand for battery storage."
    item = insertion(html, "battery storage")
    assert_refused(html, insert(html, item), Code.UNMAPPABLE_SPAN)


# A browser enters "script data double escaped" on <!-- ... <script inside <script> raw text and
# keeps reading script text past the first </script>; html.parser ends the element there. The
# <p> after the first </script> is an element for us but SCRIPT TEXT in a browser (QA L2).
DOUBLE_ESCAPED = (
    '<main><p>x</p><script>var a="<!--<script>";</script><p>EV charging market</p></script></main>'
)


@pytest.mark.parametrize(
    "html",
    [
        DOUBLE_ESCAPED,
        DOUBLE_ESCAPED.replace("<script>var", "<SCRIPT type='text/javascript'>var"),
        DOUBLE_ESCAPED.replace('"<!--<script>"', '"<!-- x <ScRiPt src=y>"'),
        DOUBLE_ESCAPED.replace('"<!--<script>"', '"<!--" + "\\n<script/"'),
    ],
)
def test_a_double_escaping_script_makes_every_insertion_unmappable(html: str) -> None:
    item = insertion(html, "EV charging")
    assert_refused(html, insert(html, item), Code.UNMAPPABLE_SPAN)


def test_a_double_escaping_script_refuses_removal_too() -> None:
    item = insertion(DOUBLE_ESCAPED, "EV charging")
    deployed = wrapped(DOUBLE_ESCAPED, "EV charging", item)
    result = MUTATOR.remove_links(deployed, [removal(item)])
    assert codes(result) == [Code.UNMAPPABLE_SPAN.value]
    assert result.html == deployed and result.applied == ()


@pytest.mark.parametrize(
    "script",
    [
        'var a="<!--";',  # escaped state only: ends at the first </script> in a browser too
        "<!-- legacy hiding -->",
        'var s="<script>"; var c="<!--";',  # <script BEFORE <!--: no double escape
    ],
)
def test_a_script_with_a_comment_opener_alone_still_works(script: str) -> None:
    html = f"<main><p>x</p><script>{script}</script><p>EV charging market</p></main>"
    item = insertion(html, "EV charging")
    result = insert(html, item)
    assert result.ok, codes(result)
    assert result.html == wrapped(html, "EV charging", item)
    assert_round_trip(html, item)


def test_cdata_text_is_unmappable() -> None:
    html = "<main><p><![CDATA[Demand for battery storage.]]></p></main>"
    item = insertion(html, "battery storage")
    assert_refused(html, insert(html, item), Code.UNMAPPABLE_SPAN)


# --------------------------------------------------------------------------- #
# Malformed markup
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "html",
    [
        "<main><p>Demand for battery storage is rising.<p>Second paragraph.</main>",
        "<main><p>Demand for battery storage is rising.</div></span></p></main>",
        "<html><body><main><p class=lead id=x>Demand for battery storage is rising.",
        "<main><p>Demand for battery storage is rising. 3 < 4 && 5 > 2</p></main>",
        "<MAIN><P>Demand for battery storage is rising.</P></MAIN>",
        "<main><p>Demand for battery storage <unknown-tag>is</unknown-tag> rising.</p>",
        "\ufeff<main><p>Demand for battery storage\x00 is rising.</p></main>",
    ],
)
def test_malformed_markup_still_splices_exactly(html: str) -> None:
    item = insertion(html, "battery storage")
    result = insert(html, item)
    assert result.ok, codes(result)
    assert result.html == wrapped(html, "battery storage", item)
    assert_round_trip(html, item)


def test_self_closing_link_before_the_span_is_unsafe() -> None:
    # html.parser closes <a/>; a browser does not, and would extend it over our text.
    html = '<main><p><a name="top"/>Demand for battery storage is rising.</p></main>'
    item = insertion(html, "battery storage")
    assert_refused(html, insert(html, item), Code.UNSAFE_PLACEMENT)


def test_unclosed_link_earlier_in_the_page_is_unsafe() -> None:
    html = f'<main><p><a href="{OTHER_HREF}">See this</p><p>Demand for battery storage.</p></main>'
    item = insertion(html, "battery storage")
    assert_refused(html, insert(html, item), Code.UNSAFE_PLACEMENT)


def test_unclosed_link_after_the_span_does_not_matter() -> None:
    html = f'<main><p>Demand for battery storage.</p><p><a href="{OTHER_HREF}">See this</p></main>'
    assert insert(html, insertion(html, "battery storage")).ok


# --------------------------------------------------------------------------- #
# Locating the block and the anchor
# --------------------------------------------------------------------------- #
def test_changed_context_is_block_not_found() -> None:
    item = insertion(PARA, "battery storage")
    edited = PARA.replace("is rising", "keeps rising")
    assert_refused(edited, insert(edited, item), Code.BLOCK_NOT_FOUND)


def test_markup_only_changes_keep_the_block() -> None:
    item = insertion(PARA, "battery storage")
    reformatted = PARA.replace("<p>Demand", '<p class="lead"  data-x=1>\n Demand')
    assert insert(reformatted, item).ok


IDENTICAL = (
    "<main><p>Battery storage demand is rising.</p><p>Something else entirely.</p>"
    "<p>Battery storage demand is rising.</p></main>"
)


def test_identical_blocks_without_a_matching_hint_are_ambiguous() -> None:
    item = insertion(IDENTICAL, "battery storage")
    from dataclasses import replace

    unhinted = replace(item, block_id_hint="b99")
    assert_refused(IDENTICAL, insert(IDENTICAL, unhinted), Code.AMBIGUOUS_BLOCK)


def test_identical_blocks_use_the_block_id_hint() -> None:
    blocks = extract_blocks(IDENTICAL)
    item = insertion(IDENTICAL, "battery storage", block=blocks[2])
    result = insert(IDENTICAL, item)
    assert result.ok
    assert result.html == wrapped(IDENTICAL, "Battery storage", item, nth=1)


def test_identical_blocks_with_our_link_in_one_are_already_applied() -> None:
    blocks = extract_blocks(IDENTICAL)
    item = insertion(IDENTICAL, "battery storage", block=blocks[2])
    applied = insert(IDENTICAL, item).html
    from dataclasses import replace

    again = insert(applied, replace(item, block_id_hint="b99"))
    assert again.ok and again.html == applied and again.already_applied == (item.plan_id,)


@pytest.mark.parametrize(
    "change",
    [
        {"occurrence_index": 5},
        {"occurrence_index": -1},
        {"start": 1},
        {"anchor_text": "BATTERY STORAGE"},
        {"anchor_text": "   "},
        {"occurrence_index": 1},
    ],
)
def test_anchor_must_match_the_occurrence_exactly(change: dict[str, object]) -> None:
    from dataclasses import replace

    item = replace(insertion(PARA, "battery storage"), **change)  # type: ignore[arg-type]
    assert_refused(PARA, insert(PARA, item), Code.ANCHOR_NOT_FOUND)


def test_fragment_without_main_and_full_document_locate_the_same_block() -> None:
    item = insertion(PARA, "battery storage")
    for html in (
        PARA.replace("<main>", "").replace("</main>", ""),
        "<!doctype html><html><head><title>T</title></head><body><header><nav>"
        "<a href='/'>Home</a></nav></header>" + PARA + "<footer>(c)</footer></body></html>",
    ):
        assert insert(html, item).html == wrapped(html, "battery storage", item)


# --------------------------------------------------------------------------- #
# Idempotence and conflicts
# --------------------------------------------------------------------------- #
def test_already_applied_is_a_no_op() -> None:
    item = insertion(PARA, "battery storage")
    once = insert(PARA, item).html
    twice = insert(once, item)
    assert twice.ok and twice.html == once
    assert twice.applied == () and twice.already_applied == (item.plan_id,)


def test_already_applied_without_data_attributes() -> None:
    item = insertion(PARA, "battery storage")
    stripped = PARA.replace("battery storage", f'<a href="{HREF}">battery storage</a>', 1)
    result = insert(stripped, item)
    assert result.ok and result.html == stripped and result.already_applied == (item.plan_id,)


def test_already_applied_mixed_with_a_new_insertion() -> None:
    first = insertion(PARA, "battery storage", plan_id=1)
    second = insertion(PARA, "rooftop solar", plan_id=2, href=OTHER_HREF)
    once = insert(PARA, first).html
    both = insert(once, first, second)
    assert (
        both.ok and both.applied == (second.plan_id,) and both.already_applied == (first.plan_id,)
    )
    assert both.html == wrapped(once, "Rooftop solar", second)


def test_another_plans_link_at_the_span_conflicts() -> None:
    item = insertion(PARA, "battery storage", plan_id=1)
    other = insert(PARA, insertion(PARA, "battery storage", plan_id=2)).html
    assert_refused(other, insert(other, item), Code.CONFLICTING_LINK)


def test_our_id_on_a_changed_link_conflicts() -> None:
    item = insertion(PARA, "battery storage")
    once = insert(PARA, item).html
    moved = once.replace(f'href="{HREF}"', f'href="{OTHER_HREF}"')
    assert_refused(moved, insert(moved, item), Code.CONFLICTING_LINK)


def test_our_id_elsewhere_on_the_page_conflicts() -> None:
    item = insertion(PARA, "battery storage")
    tag = link_start_tag(HREF, item.plan_id)
    elsewhere = PARA.replace("Rooftop solar", f"{tag}Rooftop solar</a>")
    assert_refused(elsewhere, insert(elsewhere, item), Code.CONFLICTING_LINK)


def test_our_id_with_the_block_gone_conflicts() -> None:
    item = insertion(PARA, "battery storage")
    once = insert(PARA, item).html
    edited = once.replace("is rising", "keeps rising")
    assert_refused(edited, insert(edited, item), Code.CONFLICTING_LINK)


# --------------------------------------------------------------------------- #
# href handling
# --------------------------------------------------------------------------- #
def test_href_is_attribute_escaped_and_round_trips() -> None:
    href = "https://research.example.com/r?q=\"a\"&b=<c>&d='e'"
    item = insertion(PARA, "battery storage", href=href)
    result = insert(PARA, item)
    assert result.ok
    assert (
        'href="https://research.example.com/r?q=&quot;a&quot;&amp;b=&lt;c&gt;&amp;d=&#x27;e&#x27;"'
        in result.html
    )
    assert insert(result.html, item).already_applied == (item.plan_id,)
    assert MUTATOR.remove_links(result.html, [removal(item)]).html == PARA


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        " javascript:alert(1)",
        "java\tscript:alert(1)",
        "data:text/html,<script>x</script>",
        "/relative/path",
        "//research.example.com/x",
        "https://user:pw@research.example.com/x",
        "https://research.example.com/a b",
        "https:\\\\evil.example.com",
        "",
    ],
)
def test_invalid_hrefs_are_refused(href: str) -> None:
    item = insertion(PARA, "battery storage", href=href)
    assert_refused(PARA, insert(PARA, item), Code.HREF_INVALID)


def test_one_bad_plan_blocks_the_whole_batch() -> None:
    good = insertion(PARA, "battery storage", plan_id=1)
    bad = insertion(PARA, "rooftop solar", plan_id=2, href="javascript:void(0)")
    result = insert(PARA, good, bad)
    assert codes(result) == ["href_invalid"]
    assert result.html == PARA and result.applied == ()


# --------------------------------------------------------------------------- #
# Removal (rollback)
# --------------------------------------------------------------------------- #
def test_remove_after_unrelated_later_edits() -> None:
    first = insertion(PARA, "battery storage", plan_id=1)
    deployed = insert(PARA, first).html
    second = insertion(deployed, "rooftop solar", plan_id=2, href=OTHER_HREF)
    later = insert(deployed, second).html
    later = later.replace("<h1>India EV Charging</h1>", "<h1 class='t'>India EV Charging 2026</h1>")
    later = later.replace("</article>", "<p>A new paragraph &amp; more.</p></article>")
    result = MUTATOR.remove_links(later, [removal(first)])
    assert result.ok and result.applied == (first.plan_id,)
    assert result.html == later.replace(link_start_tag(HREF, first.plan_id), "", 1).replace(
        "battery storage</a>", "battery storage", 1
    )
    assert link_start_tag(OTHER_HREF, second.plan_id) in result.html


def test_remove_after_an_edit_in_the_same_paragraph_with_the_id() -> None:
    item = insertion(PARA, "battery storage")
    deployed = insert(PARA, item).html
    edited = deployed.replace("is rising across India", "keeps rising across India")
    result = MUTATOR.remove_links(edited, [removal(item)])
    assert result.ok and result.html == PARA.replace("is rising", "keeps rising")


def test_remove_when_our_href_changed_is_a_rollback_conflict() -> None:
    item = insertion(PARA, "battery storage")
    deployed = insert(PARA, item).html
    changed = deployed.replace(f'href="{HREF}"', f'href="{OTHER_HREF}"')
    assert_refused(changed, MUTATOR.remove_links(changed, [removal(item)]), Code.ROLLBACK_CONFLICT)


def test_remove_when_our_anchor_changed_is_a_rollback_conflict() -> None:
    item = insertion(PARA, "battery storage")
    deployed = insert(PARA, item).html
    changed = deployed.replace("battery storage</a>", "battery storage systems</a>", 1)
    assert_refused(changed, MUTATOR.remove_links(changed, [removal(item)]), Code.ROLLBACK_CONFLICT)
    stripped = changed.replace(f' data-ken-link-id="{item.plan_id}"', "")
    assert_refused(
        stripped, MUTATOR.remove_links(stripped, [removal(item)]), Code.ROLLBACK_CONFLICT
    )


def test_remove_when_our_link_gained_markup_is_a_rollback_conflict() -> None:
    item = insertion(PARA, "battery storage")
    deployed = insert(PARA, item).html
    changed = deployed.replace(">battery storage</a>", "><b>battery storage</b></a>", 1)
    assert_refused(changed, MUTATOR.remove_links(changed, [removal(item)]), Code.ROLLBACK_CONFLICT)


def test_remove_when_another_link_replaced_ours_is_a_rollback_conflict() -> None:
    item = insertion(PARA, "battery storage")
    other = PARA.replace("battery storage", f'<a href="{OTHER_HREF}">battery storage</a>', 1)
    assert_refused(other, MUTATOR.remove_links(other, [removal(item)]), Code.ROLLBACK_CONFLICT)


def test_remove_never_unwraps_another_plans_link() -> None:
    mine = insertion(PARA, "battery storage", plan_id=1)
    theirs = insertion(PARA, "battery storage", plan_id=2)
    deployed = insert(PARA, theirs).html
    result = MUTATOR.remove_links(deployed, [removal(mine)])
    assert result.ok and result.html == deployed and result.already_applied == (mine.plan_id,)
    edited = deployed.replace("is rising", "keeps rising")
    result = MUTATOR.remove_links(edited, [removal(mine)])
    assert result.ok and result.html == edited


def test_remove_is_idempotent() -> None:
    item = insertion(PARA, "battery storage")
    deployed = insert(PARA, item).html
    once = MUTATOR.remove_links(deployed, [removal(item)])
    twice = MUTATOR.remove_links(once.html, [removal(item)])
    assert once.html == PARA and twice.ok and twice.html == PARA
    assert twice.applied == () and twice.already_applied == (item.plan_id,)


def test_remove_without_data_attributes() -> None:
    item = insertion(PARA, "battery storage")
    stripped = PARA.replace("battery storage", f'<a  HREF="{HREF}" rel="x">battery storage</A >', 1)
    result = MUTATOR.remove_links(stripped, [removal(item)])
    assert result.ok and result.html == PARA


def test_remove_one_plan_of_two() -> None:
    first = insertion(PARA, "battery storage", plan_id=1)
    second = insertion(PARA, "rooftop solar", plan_id=2, href=OTHER_HREF)
    both = insert(PARA, first, second).html
    result = MUTATOR.remove_links(both, [removal(second)])
    assert result.ok and result.html == insert(PARA, first).html


def test_remove_when_the_block_is_gone_and_no_link_remains_is_a_no_op() -> None:
    item = insertion(PARA, "battery storage")
    rewritten = "<main><p>Completely new content.</p></main>"
    result = MUTATOR.remove_links(rewritten, [removal(item)])
    assert result.ok and result.html == rewritten and result.already_applied == (item.plan_id,)


def test_remove_when_the_block_changed_and_a_link_to_the_target_remains_conflicts() -> None:
    item = insertion(PARA, "battery storage")
    deployed = insert(PARA, item).html.replace(f' data-ken-link-id="{item.plan_id}"', "")
    edited = deployed.replace("is rising", "keeps rising")
    assert_refused(edited, MUTATOR.remove_links(edited, [removal(item)]), Code.ROLLBACK_CONFLICT)


def test_duplicate_removals_are_refused() -> None:
    item = insertion(PARA, "battery storage")
    deployed = insert(PARA, item).html
    result = MUTATOR.remove_links(deployed, [removal(item), removal(item)])
    assert_refused(deployed, result, Code.OVERLAPPING_CHANGES, Code.OVERLAPPING_CHANGES)


def test_remove_empty_batch() -> None:
    assert MUTATOR.remove_links(PARA, []).html == PARA


def test_mutator_is_stateless_and_reusable() -> None:
    item = insertion(PARA, "battery storage")
    assert HtmlMutator().insert_links(PARA, [item], excluded_ancestors=EXCLUDED) == insert(
        PARA, item
    )


def test_change_snippets_window_one_insertion() -> None:
    item = insertion(PARA, "battery storage")
    after = insert(PARA, item).html
    before_snippet, after_snippet = change_snippets(PARA, after, chars=11)
    assert before_snippet == "Demand for battery storage is rising "
    tag = link_start_tag(HREF, item.plan_id)
    assert after_snippet == f"Demand for {tag}battery storage</a> is rising "
    long_before, long_after = change_snippets("x" * 5000, "x" * 2500 + "y" * 3000 + "x" * 2500)
    assert (
        len(long_before) <= 2 * SNIPPET_CHARS + 200 and len(long_after) <= 2 * SNIPPET_CHARS + 400
    )
    same = change_snippets(PARA, PARA, chars=5)
    assert same[0] == same[1]
