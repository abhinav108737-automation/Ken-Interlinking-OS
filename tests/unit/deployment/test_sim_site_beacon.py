"""The simulated site's additive beacon injection (ADR-0015 §1, amendment 1c).

Three properties:

1. ``beacon_src=None`` renders the Phase 6 bytes EXACTLY (byte identity);
2. the tag goes before the last ``</body>``, or at the end when there is none;
3. injecting it changes no inventory block (so no content hash) and breaks no deployment
   verification -- both block walks drop ``<script>`` (§12.1).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from ken_contracts.cms import CMSPage
from ken_services.analytics.collector.beacon import BEACON_PATH
from ken_services.deployment.mutation import HtmlMutator
from ken_services.deployment.sim_site import (
    SimulatedSiteApp,
    beacon_tag,
    render_page,
    serve_simulated_site,
)
from ken_services.deployment.source_map import map_document
from ken_services.deployment.verification import HtmlLinkVerifier
from ken_services.inventory.normalize import extract_blocks
from tests.unit.deployment._support import EXCLUDED, insertion

COLLECTOR = "http://127.0.0.1:4100"
TAG = f'<script src="{COLLECTOR}/k.js" defer data-ken-collector="{COLLECTOR}/e"></script>'
FRAGMENT = (
    "<main><article><h1>India EV Charging</h1>"
    "<p>Demand for battery storage is rising across India.</p>"
    "<p>Rooftop solar and grid-scale storage are covered elsewhere.</p></article></main>"
)


def page(body: str, *, path: str = "/reports/a", title: str = "Alpha") -> CMSPage:
    url = f"https://site.test{path}"
    return CMSPage(
        page_id="sim-1", url=url, canonical_url=url, title=title, body_html=body, version="v1"
    )


def document(body: str) -> str:
    return (
        f"<!DOCTYPE html>\n<html><head><title>T</title></head>\n<body>\n{body}\n</body>\n</html>\n"
    )


# --------------------------------------------------------------------------- #
# 1. byte identity when the beacon is off
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("body", [FRAGMENT, document(FRAGMENT), "<p>bare</p>", ""])
def test_without_a_beacon_the_output_is_byte_identical_to_phase_6(body: str) -> None:
    rendered = render_page(page(body))
    assert render_page(page(body), beacon_src=None) == rendered
    if "<html" in body.lower():
        assert rendered == body  # a full document is still served verbatim
    else:
        assert rendered == (
            '<!DOCTYPE html>\n<html>\n<head>\n<meta charset="utf-8">\n'
            "<title>Alpha</title>\n"
            '<link rel="canonical" href="https://site.test/reports/a">\n'
            f"</head>\n<body>\n{body}\n</body>\n</html>\n"
        )
    assert "k.js" not in rendered


# --------------------------------------------------------------------------- #
# 2. where the tag goes
# --------------------------------------------------------------------------- #
def test_the_tag_goes_before_the_closing_body_of_a_full_document() -> None:
    rendered = render_page(page(document(FRAGMENT)), beacon_src=COLLECTOR)
    assert rendered.count(TAG) == 1
    assert rendered.index(TAG) < rendered.lower().index("</body>")
    assert rendered.replace(TAG + "\n", "") == document(FRAGMENT)


def test_the_tag_goes_before_the_closing_body_of_a_wrapped_fragment() -> None:
    rendered = render_page(page(FRAGMENT), beacon_src=COLLECTOR)
    assert rendered.count(TAG) == 1
    assert rendered.replace(TAG + "\n", "") == render_page(page(FRAGMENT))


def test_the_last_closing_body_wins() -> None:
    body = "<html><body><p>a</p></body><!-- </body> --></html>"
    rendered = render_page(page(body), beacon_src=COLLECTOR)
    assert rendered.rindex(TAG) > rendered.index("</body>")
    assert rendered.count(TAG) == 1


def test_a_document_without_a_closing_body_gets_the_tag_at_the_end() -> None:
    body = "<html><p>no body end</p>"
    rendered = render_page(page(body), beacon_src=COLLECTOR)
    assert rendered == body + TAG + "\n"


@pytest.mark.parametrize(
    "src", [COLLECTOR, COLLECTOR + "/", 'http://127.0.0.1:4100/"><script>alert(1)</script>']
)
def test_the_collector_url_is_escaped_into_the_attribute(src: str) -> None:
    tag = beacon_tag(src)
    assert tag.count('"') == 4  # exactly the two attribute values
    assert "<script>alert" not in tag.replace("&lt;script&gt;alert", "")
    assert tag.endswith('/e"></script>')
    assert BEACON_PATH in tag


# --------------------------------------------------------------------------- #
# 3. parity: no content hash moves, verification still passes
# --------------------------------------------------------------------------- #
def test_the_beacon_changes_no_inventory_block() -> None:
    plain = render_page(page(document(FRAGMENT)))
    with_beacon = render_page(page(document(FRAGMENT)), beacon_src=COLLECTOR)
    assert with_beacon != plain
    assert extract_blocks(with_beacon) == extract_blocks(plain)
    assert [b.text for b in extract_blocks(with_beacon)] == [b.text for b in extract_blocks(plain)]


def test_the_beacon_keeps_the_deployment_source_map_sound_and_unchanged() -> None:
    item = insertion(FRAGMENT, "battery storage")
    deployed = HtmlMutator().insert_links(FRAGMENT, [item], excluded_ancestors=EXCLUDED).html
    plain = render_page(page(document(deployed)))
    with_beacon = render_page(page(document(deployed)), beacon_src=COLLECTOR)

    mapped_plain, mapped_beacon = map_document(plain), map_document(with_beacon)
    assert mapped_plain.sound and mapped_beacon.sound
    assert [
        (b.block.block_id, b.block.kind, b.block.text, b.context_hash) for b in mapped_beacon.blocks
    ] == [
        (b.block.block_id, b.block.kind, b.block.text, b.context_hash) for b in mapped_plain.blocks
    ]


def test_the_beacon_breaks_no_deployment_verification() -> None:
    item = insertion(FRAGMENT, "battery storage")
    deployed = HtmlMutator().insert_links(FRAGMENT, [item], excluded_ancestors=EXCLUDED).html
    verifier = HtmlLinkVerifier()
    plain = verifier.verify_links(render_page(page(document(deployed))), [item])
    with_beacon = verifier.verify_links(
        render_page(page(document(deployed)), beacon_src=COLLECTOR), [item]
    )
    assert with_beacon == plain
    assert all(check.outcome.value in {"passed", "skipped"} for check in with_beacon)


# --------------------------------------------------------------------------- #
# The served site
# --------------------------------------------------------------------------- #
class _Source:
    def __init__(self, pages: list[CMSPage]) -> None:
        self.pages = {p.url.removeprefix("https://site.test"): p for p in pages}

    async def page_by_path(self, path: str) -> CMSPage | None:
        return self.pages.get(path)

    async def list_pages(self) -> list[CMSPage]:
        return list(self.pages.values())


async def test_the_app_serves_the_beacon_only_when_configured() -> None:
    source = _Source([page(FRAGMENT)])
    plain = SimulatedSiteApp(source, nonce="n" * 32)
    with_beacon = SimulatedSiteApp(source, nonce="n" * 32, beacon_src=COLLECTOR)
    assert await _get(plain, "/reports/a") == render_page(page(FRAGMENT)).encode()
    assert TAG.encode() in await _get(with_beacon, "/reports/a")


async def test_the_index_page_never_carries_the_beacon() -> None:
    source = _Source([page(FRAGMENT)])
    body = await _get(SimulatedSiteApp(source, nonce="n" * 32, beacon_src=COLLECTOR), "/")
    assert b"k.js" not in body  # navigation, not content


async def test_a_served_site_still_verifies_over_http() -> None:
    source = _Source([page(document(FRAGMENT))])
    async with serve_simulated_site(source, beacon_src=COLLECTOR) as base_url:
        client = httpx.AsyncClient(base_url=base_url, trust_env=False)
        async with client:
            response = await client.get("/reports/a")
    assert response.status_code == 200
    assert TAG in response.text
    assert extract_blocks(response.text) == extract_blocks(render_page(page(document(FRAGMENT))))


async def _get(app: SimulatedSiteApp, path: str) -> bytes:
    chunks: list[bytes] = []

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            chunks.append(bytes(message.get("body") or b""))

    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
    }
    await app(scope, receive, send)  # type: ignore[arg-type]
    return b"".join(chunks)
