"""Inventory fixture-corpus eval: spec-derived goldens vs the implementation.

The corpus (``tests/fixtures/inventory/``) and its goldens (``expected/*.json``)
were written from ADR-0009 and ``ken_contracts/inventory.py``, NOT from the
implementation, so this file checks the implementation against the spec:

* every fixture-site page and every valid export record is normalised with
  ``normalize_document`` and compared field by field with its golden;
* normalisation is deterministic and the content hash ignores fetch facts;
* ``v2_changed.jsonl`` changes the hash exactly where content changed;
* ``FileExportContentSource`` turns bad records into issues and a bad header
  into an exception;
* ``PublicWebContentSource`` crawls the fixture site through an in-process
  ``httpx.MockTransport`` (no sockets, no real site) and honours robots.txt,
  meta nofollow, redirects, scope, non-HTML and the query-variant cap -- and
  two crawls produce identical record sequences.

No database, no network, no model. See ``evals/inventory/README.md``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

import pytest

from ken_contracts.inventory import (
    CrawlConfig,
    IssueCode,
    SiteScope,
    SourceDocument,
    SourceIssue,
    SourceType,
)
from ken_services.inventory.urls import build_scope
from tests.support.fixture_site import (
    EXPECTED_DIR,
    EXPORTS_DIR,
    MOCK_ORIGIN,
    ORIGIN_PLACEHOLDER,
    FixtureSite,
    substitute_origin,
)

FIXED_TIME = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
LATER_TIME = datetime(2027, 1, 1, 0, 0, tzinfo=UTC)

SITE_GOLDEN: dict[str, Any] = json.loads((EXPECTED_DIR / "site_pages.json").read_text("utf-8"))[
    "pages"
]
EXPORT_GOLDEN: dict[str, Any] = json.loads((EXPECTED_DIR / "exports.json").read_text("utf-8"))
CRAWL_GOLDEN: dict[str, Any] = json.loads((EXPECTED_DIR / "crawl.json").read_text("utf-8"))

# The spec says metadata["title_source"] "records which" source supplied the
# title, but does not freeze the label vocabulary. Accept the obvious spellings
# and fold them onto the golden's labels; anything else is reported verbatim.
TITLE_SOURCE_ALIASES: dict[str | None, str] = {
    None: "none",
    "none": "none",
    "assertion": "assertion",
    "asserted": "assertion",
    "title": "title",
    "title_tag": "title",
    "<title>": "title",
    "og:title": "og_title",
    "og_title": "og_title",
    "h1": "h1",
}


# --------------------------------------------------------------------------- #
# Module access. Guards exist only so the corpus can land before the code;
# once the modules exist (they must by the end of Phase 1) nothing skips.
# --------------------------------------------------------------------------- #
def _normalize() -> Callable[..., Any]:
    module: ModuleType = pytest.importorskip("ken_services.inventory.normalize")
    fn: Callable[..., Any] = module.normalize_document
    return fn


def _file_source_cls() -> Any:
    return pytest.importorskip("ken_services.inventory.sources.file_export").FileExportContentSource


def _web_source_cls() -> Any:
    return pytest.importorskip("ken_services.inventory.sources.public_web").PublicWebContentSource


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _o(value: Any, origin: str) -> Any:
    """Substitute ``{{ORIGIN}}`` recursively in a golden value."""
    if isinstance(value, str):
        return substitute_origin(value, origin)
    if isinstance(value, list):
        return [_o(item, origin) for item in value]
    return value


def _path_q(url: str | None) -> str:
    parts = urlsplit(url or "")
    return parts.path + (f"?{parts.query}" if parts.query else "")


def _golden_links(golden: dict[str, Any], origin: str) -> list[list[Any]]:
    if "links_generated" in golden:
        gen = golden["links_generated"]
        kind, url, anchor, source, rel = gen["template"]
        return [
            [
                kind,
                _o(url, origin).replace("{n}", str(n)),
                anchor.replace("{n}", str(n)),
                source,
                rel,
            ]
            for n in range(gen["from"], gen["to"] + 1)
        ]
    return [_o(link, origin) for link in golden["links"]]


def _actual_view(page: Any) -> dict[str, Any]:
    """The golden-shaped projection of a NormalizedPage."""
    raw_source = page.metadata.get("title_source")
    return {
        "title": page.title,
        "title_source": TITLE_SOURCE_ALIASES.get(raw_source, f"<unrecognised:{raw_source}>"),
        "canonical_status": page.canonical_status.value,
        "canonical_url": page.canonical_url,
        "language": page.language,
        "publication_state": page.publication_state.value,
        "robots": sorted(page.robots_directives),
        "headings": [[h.level, h.text] for h in page.headings],
        "links": [
            [
                link.link_type.value,
                link.resolved_url,
                link.anchor_text,
                link.anchor_source,
                list(link.rel),
            ]
            for link in page.links
        ],
    }


def _expected_view(golden: dict[str, Any], origin: str) -> dict[str, Any]:
    return {
        "title": golden["title"],
        "title_source": golden["title_source"],
        "canonical_status": golden["canonical_status"],
        "canonical_url": _o(golden["canonical_url"], origin),
        "language": golden["language"],
        "publication_state": golden["publication_state"],
        "robots": sorted(golden["robots"]),
        "headings": golden["headings"],
        "links": _golden_links(golden, origin),
    }


def _site_document(site: FixtureSite, path: str, **overrides: Any) -> SourceDocument:
    route = site.resolve(path)
    url = site.url(path)
    fields: dict[str, Any] = {
        "source_type": SourceType.PUBLIC_WEB,
        "source_id": url,
        "requested_url": url,
        "final_url": url,
        "http_status": 200,
        "content_type": route.content_type,
        "content_language": route.header("content-language"),
        "fetched_at": FIXED_TIME,
        "depth": 0,
        "html": route.text(),
    }
    fields.update(overrides)
    return SourceDocument(**fields)


def _site_scope(site: FixtureSite) -> SiteScope:
    return build_scope(site.origin)


async def _read_export(path: Path) -> list[SourceDocument | SourceIssue]:
    source = _file_source_cls()(path, max_bytes=5_000_000, clock=lambda: FIXED_TIME)
    try:
        await source.site_scope()
        return [record async for record in source.iter_records()]
    finally:
        await source.aclose()


def _export_scope() -> SiteScope:
    return SiteScope(allowed_hosts=frozenset(EXPORT_GOLDEN["scope_hosts"]))


def _export_pages(name: str) -> dict[str, Any]:
    normalize = _normalize()
    records = asyncio.run(_read_export(EXPORTS_DIR / name))
    docs = [r for r in records if isinstance(r, SourceDocument)]
    return {doc.final_url: normalize(doc, scope=_export_scope()) for doc in docs}


# --------------------------------------------------------------------------- #
# 1. Fixture site: normalisation goldens
# --------------------------------------------------------------------------- #
def test_every_site_page_has_a_golden() -> None:
    served = {route.path for route in FixtureSite().html_pages()}
    assert served == set(SITE_GOLDEN)


@pytest.mark.parametrize("path", sorted(SITE_GOLDEN))
def test_site_page_matches_golden(path: str) -> None:
    normalize = _normalize()
    site = FixtureSite()
    page = normalize(_site_document(site, path), scope=_site_scope(site))
    assert _actual_view(page) == _expected_view(SITE_GOLDEN[path], site.origin)
    assert [link.ordinal for link in page.links] == list(range(len(page.links)))


@pytest.mark.parametrize("path", sorted(SITE_GOLDEN))
def test_site_page_is_deterministic_and_hash_ignores_fetch_facts(path: str) -> None:
    normalize = _normalize()
    site = FixtureSite()
    scope = _site_scope(site)
    first = normalize(_site_document(site, path), scope=scope)
    second = normalize(_site_document(site, path), scope=scope)
    assert first == second
    assert first.model_dump_json() == second.model_dump_json()

    # Same content fetched later, via a redirect, at another depth: same hash.
    refetched = normalize(
        _site_document(
            site,
            path,
            fetched_at=LATER_TIME,
            requested_url=site.url("/old"),
            redirect_chain=(site.url("/old"),),
            depth=3,
        ),
        scope=scope,
    )
    assert refetched.content_hash == first.content_hash


def test_site_goldens_hold_on_another_origin() -> None:
    """The site is host-agnostic: a 127.0.0.1:port origin gives the same results."""
    normalize = _normalize()
    site = FixtureSite(origin="http://127.0.0.1:8765")
    for path in ("/reports/x", "/guides/base-demo", "/reports/alias-x"):
        page = normalize(_site_document(site, path), scope=_site_scope(site))
        assert _actual_view(page) == _expected_view(SITE_GOLDEN[path], site.origin), path


# --------------------------------------------------------------------------- #
# 2. JSONL exports
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("url", sorted(k for k in EXPORT_GOLDEN["valid"] if not k.startswith("_")))
def test_export_record_matches_golden(url: str) -> None:
    pages = _export_pages("valid.jsonl")
    assert url in pages, f"no document for {url}; got {sorted(pages)}"
    page = pages[url]
    golden = EXPORT_GOLDEN["valid"][url]
    assert _actual_view(page) == _expected_view(golden, "")

    def _dt(value: str | None) -> datetime | None:
        return None if value is None else datetime.fromisoformat(value)

    assert page.source_type is SourceType.FILE_EXPORT
    assert page.external_id == golden["external_id"]
    assert page.page_type == golden["page_type"]
    assert page.published_at == _dt(golden["published_at"])
    assert page.modified_at == _dt(golden["modified_at"])
    for key, value in golden["metadata_includes"].items():
        assert page.metadata.get(key) == value, key


def test_valid_export_yields_only_documents_in_file_order() -> None:
    records = asyncio.run(_read_export(EXPORTS_DIR / "valid.jsonl"))
    assert all(isinstance(r, SourceDocument) for r in records), records
    expected = [k for k in EXPORT_GOLDEN["valid"] if not k.startswith("_")]
    assert [r.final_url for r in records if isinstance(r, SourceDocument)] == expected


def test_export_read_and_normalise_are_deterministic() -> None:
    first = asyncio.run(_read_export(EXPORTS_DIR / "valid.jsonl"))
    second = asyncio.run(_read_export(EXPORTS_DIR / "valid.jsonl"))
    assert [r.model_dump(mode="json") for r in first] == [r.model_dump(mode="json") for r in second]
    a, b = _export_pages("valid.jsonl"), _export_pages("valid.jsonl")
    assert {u: p.model_dump_json() for u, p in a.items()} == {
        u: p.model_dump_json() for u, p in b.items()
    }


def test_v2_changed_hash_expectations() -> None:
    v1, v2 = _export_pages("valid.jsonl"), _export_pages("v2_changed.jsonl")
    spec = EXPORT_GOLDEN["v2_changed"]
    assert set(v1) == set(v2) == set(spec["hash_changes"]) | set(spec["hash_unchanged"])
    changed = sorted(u for u in v1 if v1[u].content_hash != v2[u].content_hash)
    assert changed == sorted(spec["hash_changes"])


def test_export_with_errors_yields_issues_not_exceptions() -> None:
    records = asyncio.run(_read_export(EXPORTS_DIR / "with_errors.jsonl"))
    actual: list[list[str]] = []
    for record in records:
        if isinstance(record, SourceDocument):
            actual.append(["document", record.final_url])
        else:
            actual.append(["issue", record.code.value])
    assert actual == EXPORT_GOLDEN["with_errors"]["sequence"]


def test_export_bad_header_raises() -> None:
    async def _consume() -> None:
        await _read_export(EXPORTS_DIR / "bad_header.jsonl")

    # Any exception is a correct "cannot start"; a skip (BaseException) propagates.
    with pytest.raises(Exception) as excinfo:
        asyncio.run(_consume())
    assert not isinstance(excinfo.value, AssertionError)


# --------------------------------------------------------------------------- #
# 3. Crawl of the fixture site through httpx.MockTransport
# --------------------------------------------------------------------------- #
async def _no_sleep(_seconds: float) -> None:
    return None


async def _crawl_once() -> tuple[FixtureSite, list[SourceDocument | SourceIssue]]:
    site = FixtureSite()
    config = CrawlConfig(base_url=f"{MOCK_ORIGIN}/", **CRAWL_GOLDEN["config"])
    source = _web_source_cls()(
        config,
        transport=site.transport(),
        sleep=_no_sleep,
        clock=lambda: FIXED_TIME,
        monotonic=lambda: 0.0,
    )
    try:
        await source.site_scope()
        records = [record async for record in source.iter_records()]
        await source.summary()
    finally:
        await source.aclose()
    return site, records


def _run_crawl() -> tuple[FixtureSite, list[SourceDocument | SourceIssue]]:
    return asyncio.run(asyncio.wait_for(_crawl_once(), timeout=60))


@pytest.fixture(scope="module")
def crawl() -> tuple[FixtureSite, list[SourceDocument | SourceIssue]]:
    _web_source_cls()
    return _run_crawl()


def _docs(records: list[SourceDocument | SourceIssue]) -> list[SourceDocument]:
    return [r for r in records if isinstance(r, SourceDocument)]


def _issues(records: list[SourceDocument | SourceIssue]) -> list[SourceIssue]:
    return [r for r in records if isinstance(r, SourceIssue)]


TRAP_PATH: str = CRAWL_GOLDEN["trap"]["path"]


def _is_trap(path: str) -> bool:
    return path.split("?", 1)[0] == TRAP_PATH


def test_crawl_documents(crawl: tuple[FixtureSite, list[Any]]) -> None:
    _site, records = crawl
    finals = [_path_q(d.final_url) for d in _docs(records)]
    assert len(finals) == len(set(finals)), "a final URL was yielded twice"
    non_trap = sorted(p for p in finals if not _is_trap(p) or p == CRAWL_GOLDEN["trap"]["path"])
    assert non_trap == sorted(CRAWL_GOLDEN["documents"])
    for doc in _docs(records):
        assert doc.source_type is SourceType.PUBLIC_WEB
        assert doc.http_status == 200
        assert doc.fetched_at == FIXED_TIME
    sea = next(d for d in _docs(records) if _path_q(d.final_url) == "/reports/sea-ev")
    assert sea.content_language == "en-IN"


def test_crawl_redirect_chain(crawl: tuple[FixtureSite, list[Any]]) -> None:
    _site, records = crawl
    by_final = {_path_q(d.final_url): d for d in _docs(records)}
    for final, spec in CRAWL_GOLDEN["redirects"].items():
        doc = by_final[final]
        assert _path_q(doc.requested_url) == spec["requested"]
        assert [_path_q(u) for u in doc.redirect_chain] == spec["chain"]


def test_crawl_issue_codes(crawl: tuple[FixtureSite, list[Any]]) -> None:
    _site, records = crawl
    actual: dict[str, list[str]] = {}
    for issue in _issues(records):
        path = _path_q(issue.url)
        if _is_trap(path) and issue.code is IssueCode.LIMIT_REACHED:
            continue
        actual.setdefault(path, []).append(issue.code.value)
    expected = {k: v for k, v in CRAWL_GOLDEN["issues"].items() if not k.startswith("_")}
    assert {k: sorted(v) for k, v in actual.items()} == expected


def test_crawl_query_variant_cap(crawl: tuple[FixtureSite, list[Any]]) -> None:
    site, records = crawl
    trap = CRAWL_GOLDEN["trap"]
    variants = [
        p for p in (_path_q(d.final_url) for d in _docs(records)) if _is_trap(p) and "?" in p
    ]
    assert trap["min_query_variants_fetched"] <= len(variants) <= trap["max_query_variants_fetched"]
    allowed = {f"{trap['path']}?page={n}" for n in range(1, trap["linked_variants"] + 1)}
    assert set(variants) <= allowed
    requested = [p for p in site.requested_paths() if _is_trap(p) and "?" in p]
    assert len(requested) <= trap["max_query_variants_fetched"]
    limit_paths = [_path_q(i.url) for i in _issues(records) if i.code.value == trap["issue_code"]]
    assert limit_paths, "no limit_reached issue for the crawler trap"
    assert all(_is_trap(p) for p in limit_paths), limit_paths


def test_crawl_never_requests_forbidden_urls(crawl: tuple[FixtureSite, list[Any]]) -> None:
    site, _records = crawl
    methods = {method for method, _url in site.requests}
    assert methods == {CRAWL_GOLDEN["only_method"]}
    off_site = [url for _m, url in site.requests if urlsplit(url).netloc != site.netloc]
    assert off_site == []
    paths = site.requested_paths()
    for path in paths:
        bare = path.split("?", 1)[0]
        assert bare not in CRAWL_GOLDEN["never_requested_paths"], path
        assert not bare.startswith(tuple(CRAWL_GOLDEN["never_requested_prefixes"])), path
        query = path.split("?", 1)[1] if "?" in path else ""
        for param in CRAWL_GOLDEN["never_requested_query_params"]:
            assert f"{param}=" not in query, path
    for once in CRAWL_GOLDEN["requested_at_most_once"]:
        assert paths.count(once) <= 1, (once, paths.count(once))


def test_crawled_documents_hash_like_direct_normalisation(
    crawl: tuple[FixtureSite, list[Any]],
) -> None:
    """Fetch facts are not content: a crawled page hashes like the same HTML normalised directly."""
    normalize = _normalize()
    site, records = crawl
    scope = _site_scope(site)
    checked = 0
    for doc in _docs(records):
        path = _path_q(doc.final_url)
        if path not in SITE_GOLDEN:
            continue
        direct = normalize(_site_document(site, path), scope=scope)
        assert normalize(doc, scope=scope).content_hash == direct.content_hash, path
        checked += 1
    assert checked >= len(CRAWL_GOLDEN["documents"]) - 1  # all but /reports/sea-ev?region=sea


def test_two_crawls_produce_identical_record_sequences(
    crawl: tuple[FixtureSite, list[Any]],
) -> None:
    site_a, first = crawl
    site_b, second = _run_crawl()
    assert [r.model_dump(mode="json") for r in first] == [r.model_dump(mode="json") for r in second]
    # Request interleaving under concurrency may differ; the request multiset may not.
    assert sorted(site_a.requested_paths()) == sorted(site_b.requested_paths())


def test_golden_origin_placeholder_is_only_in_site_goldens() -> None:
    """Guard against a golden accidentally hard-coding the mock origin."""
    for name in ("site_pages.json", "crawl.json", "exports.json"):
        assert MOCK_ORIGIN not in (EXPECTED_DIR / name).read_text("utf-8"), name
    assert ORIGIN_PLACEHOLDER not in (EXPECTED_DIR / "exports.json").read_text("utf-8")
