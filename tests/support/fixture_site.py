"""The static inventory fixture website, served without sockets.

``tests/fixtures/inventory/site/`` holds a small synthetic site and a
``_routes.json`` route table (status codes, redirects, content types,
``Content-Language`` headers). This module loads both and serves them through an
``httpx.MockTransport``, so crawler evals run with no network at all.

Routing rules (also stated in ``_routes.json``):

* matching is on the URL **path** only; the query string is ignored, so
  ``/trap?page=7`` serves ``trap.html``;
* a route with ``file`` serves that file (status 200 unless given);
* ``{{ORIGIN}}`` in text bodies and in ``location`` is replaced by the serving
  origin, so the site is host-agnostic;
* any other path answers the ``not_found`` response (404);
* a request for any other host answers 502 and is still logged, so a test
  can prove that no off-site URL was ever requested.

Every request is logged as ``(method, url)``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

import httpx

FIXTURE_ROOT: Final = Path(__file__).resolve().parents[1] / "fixtures" / "inventory"
SITE_DIR: Final = FIXTURE_ROOT / "site"
EXPORTS_DIR: Final = FIXTURE_ROOT / "exports"
EXPECTED_DIR: Final = FIXTURE_ROOT / "expected"
ROUTES_FILE: Final = SITE_DIR / "_routes.json"

ORIGIN_PLACEHOLDER: Final = "{{ORIGIN}}"
MOCK_ORIGIN: Final = "http://site.fixture.test"
"""Origin served by :meth:`FixtureSite.transport` (``.test`` is a reserved TLD)."""

_HTML: Final = "text/html; charset=utf-8"
_CONTENT_TYPES: Final = {".html": _HTML, ".txt": "text/plain; charset=utf-8"}


@dataclass(frozen=True, slots=True)
class RouteSpec:
    """One fully resolved response of the fixture site."""

    path: str
    status: int
    headers: tuple[tuple[str, str], ...]
    """Response headers, including Content-Type and Location when present."""
    body: bytes
    file: str | None = None
    """Source file under ``site/``, relative, or None for a generated response."""

    @property
    def content_type(self) -> str | None:
        return self.header("content-type")

    @property
    def location(self) -> str | None:
        return self.header("location")

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        return next((value for key, value in self.headers if key.lower() == wanted), None)

    @property
    def is_html_page(self) -> bool:
        return self.status == 200 and (self.content_type or "").startswith("text/html")

    def text(self) -> str:
        return self.body.decode("utf-8")


def substitute_origin(text: str, origin: str) -> str:
    return text.replace(ORIGIN_PLACEHOLDER, origin.rstrip("/"))


def _build(path: str, spec: dict[str, Any], origin: str) -> RouteSpec:
    file_name: str | None = spec.get("file")
    content_type: str | None = spec.get("content_type")
    body = b""
    if file_name is not None:
        body = (SITE_DIR / file_name).read_bytes()
        if content_type is None:
            content_type = _CONTENT_TYPES.get(Path(file_name).suffix, "application/octet-stream")
    elif "body" in spec:
        body = str(spec["body"]).encode("utf-8")
    if content_type is not None and content_type.startswith("text/"):
        body = substitute_origin(body.decode("utf-8"), origin).encode("utf-8")

    headers: list[tuple[str, str]] = []
    if content_type is not None:
        headers.append(("Content-Type", content_type))
    if "location" in spec:
        headers.append(("Location", substitute_origin(str(spec["location"]), origin)))
    headers.extend((str(k), str(v)) for k, v in sorted(dict(spec.get("headers", {})).items()))
    return RouteSpec(
        path=path,
        status=int(spec.get("status", 200)),
        headers=tuple(headers),
        body=body,
        file=file_name,
    )


def load_routes(origin: str = MOCK_ORIGIN) -> tuple[dict[str, RouteSpec], RouteSpec]:
    """``(routes by path, not-found response)`` with ``{{ORIGIN}}`` substituted."""
    table = json.loads(ROUTES_FILE.read_text(encoding="utf-8"))
    routes = {path: _build(path, spec, origin) for path, spec in table["routes"].items()}
    return routes, _build("*", table["not_found"], origin)


@dataclass
class FixtureSite:
    """The fixture site at ``origin``, plus a log of every request it served."""

    origin: str = MOCK_ORIGIN
    routes: dict[str, RouteSpec] = field(init=False)
    not_found: RouteSpec = field(init=False)
    requests: list[tuple[str, str]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.origin = self.origin.rstrip("/")
        self.routes, self.not_found = load_routes(self.origin)

    @property
    def netloc(self) -> str:
        return urlsplit(self.origin).netloc

    def url(self, path: str) -> str:
        return f"{self.origin}{path}"

    def resolve(self, path: str) -> RouteSpec:
        """The response for a request path (query ignored)."""
        return self.routes.get(path.split("?", 1)[0] or "/", self.not_found)

    def html_pages(self) -> list[RouteSpec]:
        """Every route that serves an HTML page with status 200, sorted by path."""
        return sorted((r for r in self.routes.values() if r.is_html_page), key=lambda r: r.path)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, str(request.url)))
        if request.url.netloc.decode("ascii") != self.netloc:
            return httpx.Response(502, headers={"Content-Type": "text/plain"}, content=b"off-site")
        route = self.resolve(request.url.path)
        return httpx.Response(route.status, headers=list(route.headers), content=route.body)

    def transport(self) -> httpx.MockTransport:
        """An ``httpx`` transport serving this site in-process (sync and async clients)."""
        return httpx.MockTransport(self.handle)

    def requested_paths(self) -> list[str]:
        """On-site request targets (path plus ``?query``), in request order."""
        out: list[str] = []
        for _method, url in self.requests:
            parts = urlsplit(url)
            if parts.netloc == self.netloc:
                out.append(parts.path + (f"?{parts.query}" if parts.query else ""))
        return out
