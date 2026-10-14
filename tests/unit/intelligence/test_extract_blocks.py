"""``extract_blocks(body_html)`` reproduces the block ids of the stored page version."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ken_contracts.inventory import NormalizedPage, SourceDocument, SourceType
from ken_services.inventory.normalize import ContentBlock, extract_blocks, normalize_document
from ken_services.inventory.urls import build_scope
from tests.support.fixture_site import FixtureSite

_ORIGIN = "https://site.test"


def _normalize(html: str, url: str = f"{_ORIGIN}/page") -> NormalizedPage:
    return normalize_document(
        SourceDocument(
            source_type=SourceType.PUBLIC_WEB,
            source_id=url,
            requested_url=url,
            final_url=url,
            http_status=200,
            content_type="text/html",
            fetched_at=datetime(2026, 10, 14, tzinfo=UTC),
            depth=0,
            html=html,
        ),
        scope=build_scope(_ORIGIN if url.startswith(_ORIGIN) else url),
    )


def _assert_parity(page: NormalizedPage) -> tuple[ContentBlock, ...]:
    blocks = extract_blocks(page.body_html)
    by_id = {block.block_id: block for block in blocks}
    assert [b.block_id for b in blocks] == [f"b{n}" for n in range(len(blocks))]
    for link in page.links:
        assert link.block_id in by_id, link
        assert by_id[link.block_id].kind == link.block_kind
        assert by_id[link.block_id].text[:1000] == link.context_text
    for heading in page.headings:
        assert heading.block_id in by_id
        assert by_id[heading.block_id].kind == f"h{heading.level}"
        assert by_id[heading.block_id].text == heading.text
    assert "\n\n".join(b.text for b in blocks if b.text) == page.body_text
    return blocks


def _corpus_pages() -> list[tuple[str, NormalizedPage]]:
    site = FixtureSite()
    scope = build_scope(site.origin)
    pages = []
    for route in site.html_pages():
        url = site.url(route.path)
        doc = SourceDocument(
            source_type=SourceType.PUBLIC_WEB,
            source_id=url,
            requested_url=url,
            final_url=url,
            http_status=200,
            content_type=route.content_type,
            content_language=route.header("content-language"),
            fetched_at=datetime(2026, 10, 14, tzinfo=UTC),
            depth=0,
            html=route.text(),
        )
        pages.append((route.path, normalize_document(doc, scope=scope)))
    return pages


CORPUS = _corpus_pages()


@pytest.mark.parametrize(("path", "page"), CORPUS, ids=[path for path, _ in CORPUS])
def test_parity_over_the_phase1_fixture_corpus(path: str, page: NormalizedPage) -> None:
    blocks = _assert_parity(page)
    assert blocks or not page.body_text, path


def test_corpus_has_links_and_headings() -> None:
    assert sum(len(page.links) for _, page in CORPUS) > 50
    assert sum(len(page.headings) for _, page in CORPUS) > 10


@pytest.mark.parametrize(
    "html",
    [
        "<p>No body at all <a href='/x'>link</a></p>tail text",
        "<html><body><div>Intro<p>Inner <a href='/a'>a</a></p>Outro</div></body></html>",
        "<body><main><header>Kept <a href='/h'>h</a></header><p>x</p></main></body>",
        "<body><header>Dropped</header><div role='main'><h2>T</h2><a href='/r'>r</a></div></body>",
        "<body><article><p>a<p>b <a href='/n'>nested</a></p></article></body>",
        "<body><table><tr><td>cell <a href='/t'>t</a></td><th>head</th></tr></table></body>",
        "<body><ul><li>one<li>two <a href='/l'>l</a></ul><pre>  code\n  block </pre></body>",
        "<body><p>Caf&eacute; &amp; &lt;tags&gt; <a href='/e'>&quot;q&quot;</a></p></body>",
        "<body><p>a<br>b<img alt='i'>c</p><a href='/bare'>bare</a> text</body>",
        "<body><main><a href='/a'><a href='/b'>nested anchors</a></a></main></body>",
        "<body><p>unclosed <b>bold <i>italic</p><div>after</body>",
        "<body><p>\x00 nul</p><a href='/z'></a></body>",
        "",
    ],
)
def test_parity_on_tricky_markup(html: str) -> None:
    _assert_parity(_normalize(html))


def test_kinds_and_text() -> None:
    page = _normalize("<body><main><h1>Title</h1><p>Para <a href='/x'>x</a>.</p></main></body>")
    assert extract_blocks(page.body_html) == (
        ContentBlock(block_id="b0", kind="h1", text="Title"),
        ContentBlock(block_id="b1", kind="p", text="Para x."),
    )


def test_empty_input() -> None:
    assert extract_blocks("") == ()
