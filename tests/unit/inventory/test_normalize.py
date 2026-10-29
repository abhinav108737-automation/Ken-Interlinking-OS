"""SourceDocument -> NormalizedPage (ken_services.inventory.normalize)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from html import unescape as html_unescape
from typing import Any

import pytest

from ken_contracts.inventory import (
    CONTENT_HASH_VERSION,
    CanonicalStatus,
    LinkType,
    NormalizedPage,
    PublicationState,
    SourceAssertions,
    SourceDocument,
    SourceType,
)
from ken_services.inventory.hashing import page_content_hash
from ken_services.inventory.normalize import NormalizationError, normalize_document
from ken_services.inventory.urls import build_scope

URL = "https://example.com/reports/a"
SCOPE = build_scope("https://example.com", ["www.example.com"])
FETCHED = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _doc(html: str, **overrides: Any) -> SourceDocument:
    values: dict[str, Any] = {
        "source_type": SourceType.PUBLIC_WEB,
        "source_id": URL,
        "requested_url": URL,
        "final_url": URL,
        "http_status": 200,
        "fetched_at": FETCHED,
        "html": html,
    }
    values.update(overrides)
    return SourceDocument(**values)


def _norm(html: str, **overrides: Any) -> NormalizedPage:
    return normalize_document(_doc(html, **overrides), scope=SCOPE)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


FULL_PAGE = """<!DOCTYPE html>
<html lang="en-GB">
<head>
  <title>  India EV   Market </title>
  <meta name="description" content="  Outlook to 2030 ">
  <meta property="og:title" content="OG title">
  <meta property="og:type" content="article">
  <meta property="article:published_time" content="2026-01-15T09:00:00+00:00">
  <meta name="robots" content="index, FOLLOW">
  <link rel="canonical" href="/reports/a">
  <script>var tracking = "do not index";</script>
</head>
<body>
  <header><a href="/">Logo</a><h1>Site name</h1></header>
  <nav><ul><li><a href="/nav-1">Nav one</a></li></ul></nav>
  <main>
    <article>
      <header><h1>India EV Market</h1><p>By Analyst</p></header>
      <p>The market is <a href="/reports/b" rel="Next nofollow next">expanding</a> fast.</p>
      <aside><a href="/related">Related</a></aside>
      <h2>Drivers</h2>
      <ul><li>Incentives <a href="https://gov.example.org/policy">policy</a></li></ul>
      <!-- an HTML comment -->
      <form><input name="q"><button>Go</button></form>
      <footer>Article footer</footer>
    </article>
  </main>
  <footer><a href="/privacy">Privacy</a></footer>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# Whole-page behaviour
# --------------------------------------------------------------------------- #
def test_full_page() -> None:
    page = _norm(FULL_PAGE)
    assert page.url == URL
    assert page.canonical_url == URL
    assert page.canonical_status is CanonicalStatus.SELF
    assert page.declared_canonical_url == URL
    assert page.title == "India EV Market"
    assert page.meta_description == "Outlook to 2030"
    assert page.language == "en-gb"
    assert page.publication_state is PublicationState.PUBLISHED
    assert page.robots_directives == ("follow", "index")
    assert page.published_at == datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
    assert page.hash_version == CONTENT_HASH_VERSION
    assert page.content_hash == page_content_hash(page)
    assert page.body_text == (
        "India EV Market\n\nBy Analyst\n\nThe market is expanding fast.\n\nDrivers\n\n"
        "Incentives policy\n\nArticle footer"
    )
    assert page.word_count == len(page.body_text.split())
    assert [(h.level, h.text) for h in page.headings] == [(1, "India EV Market"), (2, "Drivers")]
    assert [link.href for link in page.links] == ["/reports/b", "https://gov.example.org/policy"]
    for removed in ("Nav one", "Related", "Privacy", "Logo", "Site name", "tracking", "Go"):
        assert removed not in page.body_text
        assert removed not in page.body_html
    assert "an HTML comment" not in page.body_html
    assert page.metadata == {
        "article:published_time": "2026-01-15T09:00:00+00:00",
        "h1": "India EV Market",
        "og:title": "OG title",
        "og:type": "article",
        "title_source": "title",
    }
    assert list(page.metadata) == sorted(page.metadata)


def test_fetch_facts_are_copied_through() -> None:
    page = _norm(
        "<p>x</p>",
        requested_url="HTTP://Example.com:80/old?utm_source=x",
        final_url="https://Example.com/reports/a#top",
        redirect_chain=("http://example.com/old", "not a url", "https://example.com/mid"),
        http_status=200,
        source_id="src-1",
    )
    assert page.requested_url == "http://example.com/old"
    assert page.url == URL
    assert page.redirect_chain == ("http://example.com/old", "https://example.com/mid")
    assert page.http_status == 200
    assert page.fetched_at == FETCHED
    assert page.source_type is SourceType.PUBLIC_WEB
    assert page.source_id == "src-1"


@pytest.mark.parametrize("field", ["final_url", "requested_url"])
def test_unnormalisable_own_url_raises_without_echo(field: str) -> None:
    secret_url = "https://admin:hunter2@example.com/x"
    with pytest.raises(NormalizationError) as excinfo:
        _norm("<p>x</p>", **{field: secret_url})
    assert "hunter2" not in str(excinfo.value)
    assert "example.com" not in str(excinfo.value)
    assert isinstance(excinfo.value, ValueError)


# --------------------------------------------------------------------------- #
# Canonical
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("head", "status", "identity", "declared"),
    [
        ('<link rel="canonical" href="https://example.com/reports/a">', "self", URL, URL),
        ('<link rel="canonical" href="/reports/a?utm_source=x#f">', "self", URL, URL),
        (
            '<link rel="canonical" href="/reports/main">',
            "declared",
            "https://example.com/reports/main",
            "https://example.com/reports/main",
        ),
        (
            '<link rel="canonical" href="https://www.example.com/reports/a">',
            "declared",
            "https://www.example.com/reports/a",
            "https://www.example.com/reports/a",
        ),
        ("", "missing", URL, None),
        (
            '<link rel="canonical" href="https://other.com/a">',
            "out_of_scope",
            URL,
            "https://other.com/a",
        ),
        ('<link rel="canonical" href="ftp://example.com/a">', "invalid", URL, None),
        ('<link rel="canonical" href="https://u:p@example.com/a">', "invalid", URL, None),
        ('<link rel="canonical" href="https://example.com:99999/">', "invalid", URL, None),
    ],
)
def test_canonical(head: str, status: str, identity: str, declared: str | None) -> None:
    page = _norm(f"<html><head>{head}</head><body><p>x</p></body></html>")
    assert page.canonical_status == CanonicalStatus(status)
    assert page.canonical_url == identity
    assert page.declared_canonical_url == declared


def test_canonical_assertion_beats_declared() -> None:
    html = '<link rel="canonical" href="/declared"><p>x</p>'
    page = _norm(html, assertions=SourceAssertions(canonical_url="https://example.com/asserted"))
    assert page.canonical_url == "https://example.com/asserted"
    assert page.canonical_status is CanonicalStatus.DECLARED


def test_canonical_resolves_against_in_scope_base() -> None:
    html = '<base href="/docs/"><link rel="canonical" href="page"><p>x</p>'
    assert _norm(html).canonical_url == "https://example.com/docs/page"


# --------------------------------------------------------------------------- #
# Title, description, dates, metadata
# --------------------------------------------------------------------------- #
def test_title_fallbacks() -> None:
    og = _norm('<meta property="og:title" content=" OG  Title "><h1>Heading</h1>')
    assert (og.title, og.metadata["title_source"]) == ("OG Title", "og:title")

    h1 = _norm("<title>   </title><h1> Main  heading </h1><p>x</p>")
    assert (h1.title, h1.metadata["title_source"]) == ("Main heading", "h1")
    assert h1.metadata["h1"] == "Main heading"

    none = _norm("<p>No title anywhere</p>")
    assert none.title is None
    assert "title_source" not in none.metadata
    assert "h1" not in none.metadata


def test_title_assertion_wins() -> None:
    page = _norm("<title>Parsed</title>", assertions=SourceAssertions(title="  Asserted  "))
    assert page.title == "Asserted"
    assert page.metadata["title_source"] == "assertion"


def test_svg_title_is_not_the_document_title() -> None:
    page = _norm("<body><svg><title>Icon</title></svg><h1>Real</h1></body>")
    assert page.title == "Real"


def test_h1_prefers_content_over_page_header() -> None:
    html = "<body><header><h1>Site</h1></header><main><h1>Article</h1></main></body>"
    assert _norm(html).metadata["h1"] == "Article"
    # with no h1 in the content, the document's first h1 is used
    html = "<body><header><h1>Site</h1></header><main><p>x</p></main></body>"
    page = _norm(html)
    assert page.metadata["h1"] == "Site"
    assert page.title == "Site"


def test_meta_description_precedence() -> None:
    both = '<meta name="description" content="Desc"><meta property="og:description" content="OG">'
    assert _norm(both).meta_description == "Desc"
    assert _norm('<meta property="og:description" content="OG">').meta_description == "OG"
    assert _norm('<meta name="description" content="  ">').meta_description is None
    asserted = _norm(both, assertions=SourceAssertions(meta_description="Asserted"))
    assert asserted.meta_description == "Asserted"


def test_dates_from_assertions_then_meta() -> None:
    asserted = datetime(2025, 5, 5, tzinfo=UTC)
    html = (
        '<meta property="article:published_time" content="2026-01-01T00:00:00Z">'
        '<meta property="article:modified_time" content="2026-02-01T00:00:00+05:30">'
    )
    page = _norm(html)
    assert page.published_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert page.modified_at is not None
    assert page.modified_at.utcoffset() is not None
    page = _norm(html, assertions=SourceAssertions(published_at=asserted, modified_at=asserted))
    assert (page.published_at, page.modified_at) == (asserted, asserted)


@pytest.mark.parametrize("value", ["2026-01-01T00:00:00", "yesterday", ""])
def test_naive_or_unparseable_meta_dates_are_ignored(value: str) -> None:
    page = _norm(f'<meta property="article:published_time" content="{value}">')
    assert page.published_at is None


def test_page_type_and_external_id_only_from_assertions() -> None:
    html = '<meta property="og:type" content="article"><p>x</p>'
    assert _norm(html).page_type is None
    page = _norm(html, assertions=SourceAssertions(page_type="report", external_id="cms-1"))
    assert page.page_type == "report"
    assert page.external_id == "cms-1"


def test_metadata_whitelist_truncation_and_assertion_merge() -> None:
    long_value = "word " * 400
    html = (
        f'<meta property="og:description" content="{long_value}">'
        '<meta name="og:locale" content="en_GB">'
        '<meta property="og:image" content="https://example.com/i.png">'
        '<meta name="keywords" content="a,b">'
        '<meta property="og:url" content="   ">'
    )
    page = _norm(
        html,
        assertions=SourceAssertions(metadata={"og:locale": "  fr_FR  ", "template": "report"}),
    )
    assert len(page.metadata["og:description"]) == 1000
    assert page.metadata["og:locale"] == "fr_FR"
    assert page.metadata["template"] == "report"
    assert "og:image" not in page.metadata
    assert "keywords" not in page.metadata
    assert "og:url" not in page.metadata
    assert list(page.metadata) == sorted(page.metadata)


# --------------------------------------------------------------------------- #
# Language and publication state
# --------------------------------------------------------------------------- #
def test_language_precedence() -> None:
    html = '<html lang="de"><head><meta http-equiv="Content-Language" content="es"></head></html>'
    assert _norm(html, assertions=SourceAssertions(language="FR-ca")).language == "fr-ca"
    assert _norm(html, content_language="it").language == "de"
    no_lang = '<html><head><meta http-equiv="content-language" content="es, en"></head></html>'
    assert _norm(no_lang, content_language="pt-BR, en").language == "pt-br"
    assert _norm(no_lang).language == "es"
    assert _norm("<p>x</p>").language is None


@pytest.mark.parametrize("value", ["en_US", "english-language-tag", "e", "12", "en-", " "])
def test_invalid_language_is_ignored(value: str) -> None:
    assert _norm(f'<html lang="{value}"><p>x</p></html>').language is None


def test_invalid_language_falls_through_to_next_source() -> None:
    assert _norm('<html lang="en_US"><p>x</p></html>', content_language="en").language == "en"


def test_publication_state() -> None:
    assert _norm("<p>x</p>").publication_state is PublicationState.PUBLISHED
    assert _norm("<p>x</p>", http_status=203).publication_state is PublicationState.UNKNOWN
    file_doc = _norm("<p>x</p>", source_type=SourceType.FILE_EXPORT, http_status=None)
    assert file_doc.publication_state is PublicationState.UNKNOWN
    asserted = _norm(
        "<p>x</p>", assertions=SourceAssertions(publication_state=PublicationState.DRAFT)
    )
    assert asserted.publication_state is PublicationState.DRAFT


# --------------------------------------------------------------------------- #
# Boilerplate and content root
# --------------------------------------------------------------------------- #
def test_boilerplate_removed_without_main() -> None:
    html = """<body>
      <header>Top banner</header>
      <div role="navigation">Role nav</div>
      <div role="banner">Role banner</div>
      <div role="contentinfo">Role info</div>
      <div role="complementary">Role comp</div>
      <div role="search">Role search</div>
      <div aria-hidden="true">Hidden aria</div>
      <div hidden>Hidden attr</div>
      <noscript>Noscript</noscript><template>Tmpl</template><iframe>Frame</iframe>
      <select><option>Opt</option></select><textarea>Text area</textarea>
      <style>.x{}</style>
      <p>Body content</p>
      <footer>Bottom</footer>
    </body>"""
    page = _norm(html)
    assert page.body_text == "Body content"


def test_header_and_footer_inside_article_are_kept() -> None:
    html = """<body><header>Site header</header>
      <article><header><p>Article header</p></header><p>Body</p>
      <footer><p>Article footer</p></footer></article>
      <footer>Site footer</footer></body>"""
    page = _norm(html)
    assert page.body_text == "Article header\n\nBody\n\nArticle footer"


def test_loose_header_text_joins_the_enclosing_container_block() -> None:
    # header/footer are not containers: loose text in them belongs to the article.
    html = "<article><header>By A</header><p>Body</p><footer>Tags</footer></article>"
    assert _norm(html).body_text == "By A Tags\n\nBody"


def test_header_inside_article_inside_body_root_is_kept() -> None:
    # root is <main>; the article inside it keeps its header and footer.
    html = "<main><article><header>H</header><p>P</p></article><footer>F</footer></main>"
    assert _norm(html).body_text == "H\n\nP\n\nF"


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("<body><main><p>In main</p></main><article><p>Art</p></article></body>", "In main"),
        ("<body><p>Out</p><article><p>Art</p></article></body>", "Art"),
        ('<body><p>Out</p><div role="main"><p>Role</p></div></body>', "Role"),
        ("<body><p>Body only</p></body>", "Body only"),
        ("<p>Fragment</p> trailing text", "Fragment\n\ntrailing text"),
    ],
)
def test_content_root_selection(html: str, expected: str) -> None:
    assert _norm(html).body_text == expected


def test_document_without_body_ignores_head() -> None:
    page = _norm("<html><head><title>T</title><style>x</style></head><p>Text</p></html>")
    assert page.body_text == "Text"
    assert page.title == "T"
    assert "<title>" not in page.body_html


@pytest.mark.parametrize(
    "html",
    [
        "<html><body><p>unclosed <b>bold <div>block <i>italic",
        "</div></span><p>stray end tags</p></table></body></html>",
        "<table><tr><td>a<table><tr><td>nested<a href='/n'>n</a></table></td><td>b</tr></table>",
        "<a href='/x'<p>broken attribute",
        "<!-- unterminated comment",
        "<![if !supportLists]>conditional<![endif]>",
        "<\x00>&#xFFFFFFFF;<!DOCTYPE",
        "",
        "plain text, no tags at all",
        # A short id, because the value is ~36 kB: pytest puts the test id in
        # PYTEST_CURRENT_TEST, and Windows refuses an environment variable longer than
        # 32767 characters, erroring the case at setup before the parser is reached.
        pytest.param("<div>" * 3000 + "deep" + "</div>" * 3000, id="3000-nested-divs"),
    ],
)
def test_malformed_html_never_raises(html: str) -> None:
    page = _norm(html)
    assert page.content_hash == page_content_hash(page)


def test_nested_tables_blocks() -> None:
    html = (
        "<table><tr><td>outer<table><tr><td>inner</td></tr></table></td><th>head</th></tr></table>"
    )
    page = _norm(html)
    assert page.body_text == "outer\n\ninner\n\nhead"


# --------------------------------------------------------------------------- #
# Blocks
# --------------------------------------------------------------------------- #
def test_nested_blocks_innermost_wins() -> None:
    html = (
        "<ul><li>item <a href='/a'>a</a><p>para <a href='/b'>b</a></p>"
        "tail <a href='/c'>c</a></li></ul>"
    )
    page = _norm(html)
    assert page.body_text == "item a tail c\n\npara b"
    by_href = {link.href: link for link in page.links}
    assert [link.href for link in page.links] == ["/a", "/b", "/c"]
    assert by_href["/a"].block_kind == "li"
    assert by_href["/b"].block_kind == "p"
    assert by_href["/c"].block_kind == "li"
    assert by_href["/a"].block_id == by_href["/c"].block_id == "b0"
    assert by_href["/b"].block_id == "b1"
    assert (by_href["/a"].occurrence_index, by_href["/c"].occurrence_index) == (0, 1)
    assert by_href["/b"].occurrence_index == 0
    assert [link.ordinal for link in page.links] == [0, 1, 2]


def test_implicit_container_blocks_and_inline_text_inside_blocks() -> None:
    html = (
        "<div>Loose <span>text</span><section>Sec <p>Para</p></section><li>a<div>b</div></li></div>"
    )
    page = _norm(html)
    assert page.body_text == "Loose text\n\nSec\n\nPara\n\na b"


def test_block_ids_count_empty_link_bearing_blocks() -> None:
    html = (
        "<p>First</p><p>   </p>"
        "<p><a href='/img'><img alt=''></a></p>"
        "<h2><a href='/h'></a></h2>"
        "<p>Last <a href='/l'>l</a></p>"
    )
    page = _norm(html)
    assert page.body_text == "First\n\nLast l"
    assert [(link.href, link.block_id, link.block_kind) for link in page.links] == [
        ("/img", "b1", "p"),
        ("/h", "b2", "h2"),
        ("/l", "b3", "p"),
    ]
    assert page.headings == ()  # an empty h2 is not a heading
    assert page.links[0].context_text == ""
    assert page.links[0].context_hash == _sha("")


def test_headings_have_block_ids() -> None:
    page = _norm("<h1>One</h1><p>x</p><h3>Three <a href='/t'>t</a></h3>")
    assert [(h.level, h.text, h.block_id) for h in page.headings] == [
        (1, "One", "b0"),
        (3, "Three t", "b2"),
    ]
    assert page.links[0].block_id == "b2"


def test_text_is_nfc_and_whitespace_collapsed() -> None:
    decomposed = "Cafe\u0301"  # e + combining acute accent
    page = _norm(f"<p>  {decomposed}\n\t au\u00a0lait  </p>")  # includes a no-break space
    assert page.body_text == "Caf\u00e9 au lait"


def test_context_text_truncated_but_hash_covers_full_text() -> None:
    text = "word " * 500
    page = _norm(f"<p>{text}<a href='/x'>x</a></p>")
    link = page.links[0]
    full = " ".join((text + " x").split())
    assert link.context_text == full[:1000]
    assert len(link.context_text) == 1000
    assert link.context_hash == _sha(full)


# --------------------------------------------------------------------------- #
# Links
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("href", "link_type", "resolved"),
    [
        ("/reports/b", LinkType.INTERNAL, "https://example.com/reports/b"),
        ("b?utm_source=x#frag", LinkType.INTERNAL, "https://example.com/reports/b"),
        ("http://example.com/x", LinkType.INTERNAL, "http://example.com/x"),
        ("https://www.example.com/x", LinkType.INTERNAL, "https://www.example.com/x"),
        ("https://other.com/x", LinkType.EXTERNAL, "https://other.com/x"),
        ("//cdn.other.com/x", LinkType.EXTERNAL, "https://cdn.other.com/x"),
        ("https://blog.example.com/", LinkType.EXTERNAL, "https://blog.example.com/"),
        ("#section", LinkType.FRAGMENT, None),
        ("#", LinkType.FRAGMENT, None),
        ("mailto:a@example.com", LinkType.NON_HTTP, None),
        ("tel:+1555", LinkType.NON_HTTP, None),
        ("javascript:void(0)", LinkType.NON_HTTP, None),
        ("JavaScript:alert(1)", LinkType.NON_HTTP, None),
        ("java\tscript:alert(1)", LinkType.NON_HTTP, None),
        ("javascript&colon;alert(1)", LinkType.NON_HTTP, None),
        ("data:text/html,hi", LinkType.NON_HTTP, None),
        ("ftp://example.com/f", LinkType.NON_HTTP, None),
        ("", LinkType.INVALID, None),
        ("   ", LinkType.INVALID, None),
        ("https://user:pw@example.com/", LinkType.INVALID, None),
        ("https://example.com:99999/", LinkType.INVALID, None),
        ("http://[::1", LinkType.INVALID, None),
    ],
)
def test_link_classification(href: str, link_type: LinkType, resolved: str | None) -> None:
    page = _norm(f'<p><a href="{href}">x</a></p>')
    assert len(page.links) == 1
    link = page.links[0]
    assert link.link_type is link_type
    assert link.resolved_url == resolved
    assert link.href == html_unescape(href).strip()  # html.parser decodes entities


def test_javascript_is_never_internal() -> None:
    hrefs = [
        "javascript:location='/x'",
        " javascript:void(0) ",
        "JAVASCRIPT:x",
        "jav&#x09;ascript:x",
    ]
    html = "".join(f'<p><a href="{href}">x</a></p>' for href in hrefs)
    page = _norm(html)
    assert len(page.links) == len(hrefs)
    assert all(link.link_type is LinkType.NON_HTTP for link in page.links)
    assert all(link.resolved_url is None for link in page.links)


def test_anchors_without_href_are_ignored_and_removed_regions_excluded() -> None:
    html = '<nav><a href="/n">n</a></nav><p><a name="x">named</a><a href="/y">y</a></p>'
    page = _norm(html)
    assert [link.href for link in page.links] == ["/y"]


def test_in_scope_base_href_is_used() -> None:
    html = '<head><base href="https://www.example.com/docs/"></head><p><a href="page">p</a></p>'
    link = _norm(html).links[0]
    assert link.resolved_url == "https://www.example.com/docs/page"
    assert link.link_type is LinkType.INTERNAL


def test_relative_base_href_resolves_against_final_url() -> None:
    html = '<head><base href="/docs/"></head><p><a href="page">p</a></p>'
    assert _norm(html).links[0].resolved_url == "https://example.com/docs/page"


def test_off_scope_base_href_is_ignored() -> None:
    html = '<head><base href="https://evil.example.net/x/"></head><p><a href="page">p</a></p>'
    link = _norm(html).links[0]
    assert link.resolved_url == "https://example.com/reports/page"
    assert link.link_type is LinkType.INTERNAL


def test_invalid_base_href_is_ignored() -> None:
    html = '<head><base href="javascript:alert(1)"></head><p><a href="page">p</a></p>'
    assert _norm(html).links[0].resolved_url == "https://example.com/reports/page"


def test_anchor_text_fallbacks() -> None:
    html = (
        "<p>"
        '<a href="/1"> Visible   text </a>'
        '<a href="/2"><img alt=""><img alt=" Second  image "></a>'
        '<a href="/3" aria-label=" Aria " title="Title"><img alt=""></a>'
        '<a href="/4" title=" Just title "></a>'
        '<a href="/5"></a>'
        "</p>"
    )
    page = _norm(html)
    assert [(link.anchor_text, link.anchor_source) for link in page.links] == [
        ("Visible text", "text"),
        ("Second image", "img_alt"),
        ("Aria", "aria_label"),
        ("Just title", "title"),
        ("", "none"),
    ]


def test_rel_tokens_sorted_unique_lower() -> None:
    page = _norm(
        '<p><a href="/a" rel=" UGC nofollow  NoFollow sponsored ">a</a><a href="/b">b</a></p>'
    )
    assert page.links[0].rel == ("nofollow", "sponsored", "ugc")
    assert page.links[1].rel == ()


# --------------------------------------------------------------------------- #
# Determinism and the content hash
# --------------------------------------------------------------------------- #
def test_same_document_twice_is_identical() -> None:
    first = _norm(FULL_PAGE)
    second = _norm(FULL_PAGE)
    assert first == second
    assert first.model_dump_json() == second.model_dump_json()
    assert first.content_hash == second.content_hash


def test_whitespace_only_formatting_change_keeps_hash() -> None:
    compact = (
        "<html><head><title>T</title></head><body><main><h1>Head</h1><p>Some "
        '<a href="/x">link</a> text.</p><ul><li>One</li><li>Two</li></ul></main></body></html>'
    )
    pretty = """
    <html>
      <head>
        <title>
          T
        </title>
      </head>
      <body>
        <main>
          <h1>  Head  </h1>
          <p>
            Some
            <a href="/x">link</a>
            text.
          </p>
          <ul>
            <li>One</li>
            <li>Two</li>
          </ul>
        </main>
      </body>
    </html>
    """
    a, b = _norm(compact), _norm(pretty)
    assert a.body_html != b.body_html
    assert a.body_text == b.body_text
    assert a.content_hash == b.content_hash


def test_changed_link_target_with_same_text_changes_hash() -> None:
    a = _norm('<p>Read the <a href="/one">report</a>.</p>')
    b = _norm('<p>Read the <a href="/two">report</a>.</p>')
    assert a.body_text == b.body_text
    assert a.content_hash != b.content_hash


def test_changed_rel_changes_hash() -> None:
    a = _norm('<p><a href="/one">r</a></p>')
    b = _norm('<p><a href="/one" rel="nofollow">r</a></p>')
    assert a.content_hash != b.content_hash


def test_fetch_facts_do_not_change_hash() -> None:
    base = _norm(FULL_PAGE)
    other = _norm(
        FULL_PAGE,
        fetched_at=datetime(2030, 1, 1, tzinfo=UTC),
        http_status=203,
        assertions=SourceAssertions(publication_state=PublicationState.PUBLISHED),
        redirect_chain=("https://example.com/old",),
        requested_url="https://example.com/old",
        source_id="another",
        content_type="text/html; charset=utf-8",
        depth=3,
    )
    assert other.content_hash == base.content_hash


def test_content_changes_change_hash() -> None:
    base = _norm(FULL_PAGE).content_hash
    assert _norm(FULL_PAGE.replace("fast.", "slowly.")).content_hash != base
    assert _norm(FULL_PAGE.replace("Outlook to 2030", "Outlook")).content_hash != base
    assert _norm(FULL_PAGE.replace('lang="en-GB"', 'lang="fr"')).content_hash != base
    assert _norm(FULL_PAGE.replace("index, FOLLOW", "noindex")).content_hash != base
    assert _norm(FULL_PAGE, http_status=203).content_hash != base  # state: published -> unknown


def test_blocks_are_ordered_by_where_their_content_starts() -> None:
    assert _norm("<div><p>A</p>B</div>").body_text == "A\n\nB"
    assert _norm("<div>\n  <p>A</p>\n  B\n</div>").body_text == "A\n\nB"
    assert _norm("<div>Before<p>A</p>After</div>").body_text == "Before After\n\nA"


def test_inline_markup_does_not_split_words() -> None:
    """Text must quote the page exactly: later phases match anchors against it (PRD 7.7)."""
    html = (
        "<main><p>Hel<b>lo</b> <a href='/x'>Electric <em>V</em>ehicles</a> market"
        "<br>next line</p></main>"
    )
    page = _norm(html)
    assert page.body_text == "Hello Electric Vehicles market next line"
    [link] = page.links
    assert link.anchor_text == "Electric Vehicles"
    assert link.context_text == "Hello Electric Vehicles market next line"
