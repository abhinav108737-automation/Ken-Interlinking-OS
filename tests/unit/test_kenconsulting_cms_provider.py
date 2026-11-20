"""KenConsultingCMSProvider: the dialect translation, the two keys, and the gating (ADR-0028).

Pure unit tests over an ``httpx.MockTransport`` that RECORDS every request; no socket is
opened. The fixtures below are built from the technology team's own guide of 2026-11-12, so a
change in their API shows up here as a failing assertion rather than as a bad write.

The trailing slash is asserted on EVERY path: Django answers a slash-less path with a 301 and
this client never follows redirects, so a missing slash is a total outage, not a slow path.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any, Final

import httpx
import pytest
from pydantic import SecretStr

from ken_contracts.cms import (
    CMSNotFoundError,
    CMSUnavailableError,
    CMSValidationError,
    CMSVersionConflictError,
    LinkChange,
    LinkChangeRequest,
    LinkLocator,
    LinkOperation,
)
from ken_core.providers.cms.http import CMSAccessRefusedError
from ken_core.providers.cms.kenconsulting import KenConsultingCMSProvider

READ_TOKEN: Final = "read-SECRET-3f91"
WRITE_TOKEN: Final = "write-SECRET-7c42"
BASE: Final = "http://127.0.0.1:18080"
SLUG: Final = "thailand-ev-charging-infrastructure-and-smart-energy-mobility-ecosystem-market"
FIELD: Final = "v02_market_overview"
PAGE: Final = f"{SLUG}:{FIELD}"
TARGET: Final = "/industry-reports/qatar-tire-distribution-and-retail-service-ecosystem-market"
VERSION: Final = "v-8f3a2b91"

type Handler = Callable[[httpx.Request], httpx.Response]


class Recorder:
    """Records each request, then answers via ``handler``."""

    def __init__(self, handler: Handler | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.handler: Handler = handler or (lambda _r: httpx.Response(500))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    def bodies(self) -> list[Any]:
        return [json.loads(r.content) if r.content else None for r in self.requests]


def provider(
    recorder: Recorder,
    *,
    read_token: str | None = READ_TOKEN,
    write_token: str | None = WRITE_TOKEN,
    writes_enabled: bool = True,
    allow_remote: bool = False,
    base_url: str | None = BASE,
) -> KenConsultingCMSProvider:
    return KenConsultingCMSProvider(
        base_url=base_url,
        read_token=None if read_token is None else SecretStr(read_token),
        write_token=None if write_token is None else SecretStr(write_token),
        writes_enabled=writes_enabled,
        allow_remote=allow_remote,
        transport=httpx.MockTransport(recorder),
    )


def change(
    *, occurrence_index: int = 0, href: str = TARGET, anchor: str = "fleet depots"
) -> LinkChange:
    return LinkChange(
        change_id=uuid.uuid4(),
        operation=LinkOperation.INSERT,
        target_page_id="qatar-tire",
        href=href,
        anchor_text=anchor,
        locator=LinkLocator(block_selector=FIELD, occurrence_index=occurrence_index),
    )


def request(*changes: LinkChange, dry_run: bool = False) -> LinkChangeRequest:
    return LinkChangeRequest(
        operation_id=uuid.uuid4(),
        source_page_id=PAGE,
        expected_version=VERSION,
        changes=list(changes) or [change()],
        dry_run=dry_run,
        actor="abhinav.kumar",
    )


def field_response(version: str = VERSION, *, protected: bool = False) -> httpx.Response:
    """``GET /pages/<slug>:<field>/``, RECORDED from the live API on 2026-11-13.

    Six keys, ``protected`` among them, which is why reading a field is one call and not two.
    """
    return httpx.Response(
        200,
        json={
            "page_id": PAGE,
            "url": f"https://thekenconsulting.com/industry-reports/{SLUG}",
            "title": "Thailand EV Charging Infrastructure and Smart Energy Mobility "
            "Ecosystem Market Assessment and Outlook to 2030",
            "body_html": "<h1>CHAPTER 1 - Market Overview</h1><p>Charging at fleet depots "
            "is growing.</p>",
            "version": version,
            "protected": protected,
        },
    )


def read_handler(*, protected: bool = False, version: str = VERSION) -> Handler:
    def handler(_r: httpx.Request) -> httpx.Response:
        return field_response(version, protected=protected)

    return handler


def validate_response(*results: dict[str, object]) -> httpx.Response:
    """``POST /link-changes/validate/``, RECORDED from the live API on 2026-11-13.

    ``valid`` is a COUNT, not a boolean. Reading it as one is the defect this fixture exists
    to prevent.
    """
    invalid = sum(1 for r in results if not r.get("valid"))
    return httpx.Response(
        200,
        json={
            "total": len(results),
            "valid": len(results) - invalid,
            "invalid": invalid,
            "results": [{"index": i, "page_id": PAGE, **r} for i, r in enumerate(results)],
        },
    )


def all_valid(n: int = 1) -> Handler:
    row: dict[str, object] = {"valid": True}
    return lambda _r: validate_response(*(row,) * n)


# ------------------------------------------------------------------ gating
@pytest.mark.asyncio
async def test_a_remote_base_url_is_refused_before_any_io() -> None:
    rec = Recorder()
    remote = provider(rec, base_url="https://www.thekenconsulting.com/app/product")
    with pytest.raises(CMSAccessRefusedError):
        await remote.get_page(page_id=PAGE)
    assert rec.requests == []


@pytest.mark.asyncio
async def test_a_write_is_refused_while_writes_are_disabled() -> None:
    rec = Recorder()
    cms = provider(rec, writes_enabled=False)
    with pytest.raises(CMSAccessRefusedError):
        await cms.apply_link_changes(request=request())
    assert rec.requests == []


@pytest.mark.asyncio
async def test_validate_needs_the_write_key_but_not_writes_enabled() -> None:
    """Their validate endpoint changes nothing and is authorised with the write key. A dry run
    must therefore work with writes still switched off -- that is what a dry run is for."""
    rec = Recorder(all_valid())
    cms = provider(rec, writes_enabled=False)
    result = await cms.validate_link_changes(request=request())
    assert result.valid
    assert rec.requests[0].headers["Authorization"] == f"Bearer {WRITE_TOKEN}"


@pytest.mark.asyncio
async def test_a_read_sends_the_read_key_and_never_the_write_key() -> None:
    rec = Recorder(read_handler())
    await provider(rec).get_page(page_id=PAGE)
    for sent in rec.requests:
        assert sent.headers["Authorization"] == f"Bearer {READ_TOKEN}"
        assert WRITE_TOKEN not in str(sent.headers)


@pytest.mark.asyncio
async def test_a_missing_read_key_is_refused_before_any_io() -> None:
    rec = Recorder()
    with pytest.raises(CMSAccessRefusedError):
        await provider(rec, read_token=None).get_page(page_id=PAGE)
    assert rec.requests == []


# --------------------------------------------------------- trailing slashes
@pytest.mark.asyncio
async def test_every_path_ends_in_a_slash() -> None:
    rec = Recorder(read_handler())
    cms = provider(rec)
    await cms.get_page(page_id=PAGE)

    rec.handler = all_valid()
    await cms.validate_link_changes(request=request())

    rec.handler = lambda r: (
        httpx.Response(200, json={"status": "applied", "deployment_id": 41, "new_version": "v2"})
        if r.url.path.endswith("/link-changes/")
        else read_handler()(r)
    )
    await cms.apply_link_changes(request=request())

    rec.handler = lambda _r: httpx.Response(200, json={"status": "rolled_back"})
    await cms.rollback(deployment_id="41", operation_id=uuid.uuid4(), actor="abhinav.kumar")

    assert rec.paths(), "no requests were recorded"
    for path in rec.paths():
        assert path.endswith("/"), f"{path} has no trailing slash; Django will 301 it"


@pytest.mark.asyncio
async def test_a_redirect_is_reported_as_the_trailing_slash_problem_it_is() -> None:
    rec = Recorder(lambda _r: httpx.Response(301, headers={"Location": "/elsewhere/"}))
    with pytest.raises(CMSUnavailableError, match="trailing slash"):
        await provider(rec).get_page(page_id=PAGE)


# -------------------------------------------------------------- reading
@pytest.mark.asyncio
async def test_get_page_is_a_single_call_carrying_protected() -> None:
    """The live API answers ``protected`` on the field itself, so one read is enough."""
    rec = Recorder(read_handler(protected=True))
    page = await provider(rec).get_page(page_id=PAGE)
    assert page.page_id == PAGE
    assert page.version == VERSION
    assert page.protected is True
    assert page.metadata == {"slug": SLUG, "field": FIELD}
    assert rec.paths() == [f"/internal/pages/{PAGE}/"]


@pytest.mark.asyncio
async def test_the_page_id_separator_is_not_escaped() -> None:
    """``:`` is their page-id separator. Escaping it addresses a page that does not exist."""
    rec = Recorder(read_handler())
    await provider(rec).get_page(page_id=PAGE)
    assert "%3A" not in rec.paths()[0]
    assert f"{SLUG}:{FIELD}" in rec.paths()[0]


@pytest.mark.asyncio
async def test_a_field_payload_without_recognised_html_fails_loudly() -> None:
    rec = Recorder(lambda _r: httpx.Response(200, json={"version": VERSION, "unexpected": "x"}))
    with pytest.raises(CMSUnavailableError, match="recognised HTML key"):
        await provider(rec).get_page(page_id=PAGE)


@pytest.mark.asyncio
async def test_a_field_without_the_protected_flag_fails_loudly() -> None:
    """A page whose protection we could not read is not a page we may edit."""
    rec = Recorder(
        lambda _r: httpx.Response(200, json={"version": VERSION, "body_html": "<p>x</p>"})
    )
    with pytest.raises(CMSUnavailableError, match="protected"):
        await provider(rec).get_page(page_id=PAGE)


# ------------------------------------------------------------- validate
@pytest.mark.asyncio
async def test_validate_sends_their_changes_array_with_a_one_based_occurrence() -> None:
    rec = Recorder(all_valid())
    req = request(change(occurrence_index=0), change(occurrence_index=2))
    await provider(rec).validate_link_changes(request=req)

    assert rec.paths() == ["/internal/link-changes/validate/"]
    body = rec.bodies()[0]
    assert [c["link"]["occurrence"] for c in body["changes"]] == [1, 3]
    assert body["changes"][0]["page_id"] == PAGE
    assert body["changes"][0]["expected_version"] == VERSION
    assert body["changes"][0]["link"]["href"] == TARGET


@pytest.mark.asyncio
async def test_a_refusal_becomes_issues_not_an_exception() -> None:
    rec = Recorder(
        lambda _r: httpx.Response(422, json={"code": "anchor_not_found", "detail": "no such text"})
    )
    result = await provider(rec).validate_link_changes(request=request())
    assert result.valid is False
    assert [i.code for i in result.issues] == ["anchor_not_found"]


@pytest.mark.asyncio
async def test_each_refusal_is_mapped_back_to_the_change_it_belongs_to() -> None:
    """Their ``index`` is the only link between a refusal and OUR change. Without the mapping
    a reviewer is told the page failed, not which of their links did."""
    first, second, third = change(), change(anchor="battery swapping"), change()
    rec = Recorder(
        lambda _r: validate_response(
            {"valid": True},
            {
                "valid": False,
                "error": "anchor_not_found",
                "message": "anchor_text occurrence 1 not found in a linkable text node",
            },
            {"valid": False, "error": "already_linked", "message": "already linked"},
        )
    )
    result = await provider(rec).validate_link_changes(request=request(first, second, third))
    assert result.valid is False
    assert [(i.code, i.change_id) for i in result.issues] == [
        ("anchor_not_found", second.change_id),
        ("already_linked", third.change_id),
    ]


@pytest.mark.asyncio
async def test_all_valid_is_a_count_of_zero_invalid_not_a_boolean() -> None:
    """``valid`` at the top level is a COUNT. Reading it as a bool would make a fully valid
    batch of two look false-y in some shapes and truthy in others."""
    rec = Recorder(all_valid(2))
    result = await provider(rec).validate_link_changes(request=request(change(), change()))
    assert result.valid is True
    assert result.issues == []


# ------------------------------------------------------------------ href
@pytest.mark.asyncio
async def test_an_absolute_site_url_is_reduced_to_a_path() -> None:
    rec = Recorder(all_valid())
    req = request(change(href=f"https://www.kenresearch.com{TARGET}"))
    await provider(rec).validate_link_changes(request=req)
    assert rec.bodies()[0]["changes"][0]["link"]["href"] == TARGET


@pytest.mark.asyncio
async def test_an_offsite_url_is_refused_before_any_io() -> None:
    rec = Recorder()
    req = request(change(href="https://example.com/whatever"))
    with pytest.raises(CMSValidationError) as caught:
        await provider(rec).validate_link_changes(request=req)
    assert [i.code for i in caught.value.issues] == ["href_not_site_relative"]
    assert rec.requests == []


# ----------------------------------------------------------------- apply
@pytest.mark.asyncio
async def test_a_dry_run_never_reaches_the_apply_endpoint() -> None:
    rec = Recorder(all_valid())
    result = await provider(rec).apply_link_changes(request=request(dry_run=True))
    assert result.status == "no_op"
    assert rec.paths() == ["/internal/link-changes/validate/"]


@pytest.mark.asyncio
async def test_one_change_is_one_call_carrying_their_single_link_object() -> None:
    rec = Recorder(
        lambda _r: httpx.Response(
            200, json={"status": "applied", "deployment_id": 41, "new_version": "v-next"}
        )
    )
    result = await provider(rec).apply_link_changes(request=request())
    assert result.status == "applied"
    assert result.deployment_id == "41"
    assert result.previous_version == VERSION
    assert result.new_version == "v-next"

    body = rec.bodies()[0]
    assert set(body) == {"operation_id", "expected_version", "link"}
    assert body["link"] == {"anchor_text": "fleet depots", "href": TARGET, "occurrence": 1}


@pytest.mark.asyncio
async def test_two_changes_are_two_calls_and_the_second_uses_the_new_version() -> None:
    """The first write invalidates the lock the second is holding."""
    versions = ["v-after-1", "v-after-2"]
    deployments = iter([41, 42])

    def handler(r: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "applied",
                "deployment_id": next(deployments),
                "new_version": versions.pop(0),
            },
        )

    rec = Recorder(handler)
    result = await provider(rec).apply_link_changes(request=request(change(), change()))

    sent = rec.bodies()
    assert len(sent) == 2
    assert sent[0]["expected_version"] == VERSION
    assert sent[1]["expected_version"] == "v-after-1"
    assert result.deployment_id == "41,42"
    assert result.previous_version == VERSION
    assert result.new_version == "v-after-2"


@pytest.mark.asyncio
async def test_a_partial_failure_rolls_back_what_was_already_applied() -> None:
    calls: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        calls.append(r.url.path)
        if r.url.path.endswith("/rollback/"):
            return httpx.Response(200, json={"status": "rolled_back"})
        if len([c for c in calls if c.endswith("/link-changes/")]) == 1:
            return httpx.Response(
                200, json={"status": "applied", "deployment_id": 41, "new_version": "v-after-1"}
            )
        return httpx.Response(422, json={"code": "anchor_not_found"})

    rec = Recorder(handler)
    with pytest.raises(CMSValidationError):
        await provider(rec).apply_link_changes(request=request(change(), change()))

    assert calls[-1] == "/internal/deployments/41/rollback/", "the applied change was not undone"


@pytest.mark.asyncio
async def test_a_version_conflict_carries_expected_and_current() -> None:
    rec = Recorder(lambda _r: httpx.Response(409, json={"current": "v-moved"}))
    with pytest.raises(CMSVersionConflictError) as caught:
        await provider(rec).apply_link_changes(request=request())
    assert caught.value.expected == VERSION
    assert caught.value.current == "v-moved"


@pytest.mark.asyncio
async def test_an_apply_without_a_deployment_id_is_off_contract() -> None:
    """Without a rollback handle the change is irreversible, which is never acceptable."""
    rec = Recorder(lambda _r: httpx.Response(200, json={"status": "applied"}))
    with pytest.raises(CMSUnavailableError, match="deployment_id"):
        await provider(rec).apply_link_changes(request=request())


@pytest.mark.asyncio
async def test_the_new_version_is_re_read_when_they_do_not_return_one() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path.endswith("/link-changes/"):
            return httpx.Response(200, json={"status": "applied", "deployment_id": 41})
        return read_handler(version="v-read-back")(r)

    rec = Recorder(handler)
    result = await provider(rec).apply_link_changes(request=request())
    assert result.new_version == "v-read-back"


# -------------------------------------------------------------- rollback
@pytest.mark.asyncio
async def test_a_composite_deployment_is_unwound_in_reverse() -> None:
    rec = Recorder(lambda _r: httpx.Response(200, json={"status": "rolled_back"}))
    result = await provider(rec).rollback(
        deployment_id="41,42,43", operation_id=uuid.uuid4(), actor="abhinav.kumar"
    )
    assert result.status == "rolled_back"
    assert rec.paths() == [
        "/internal/deployments/43/rollback/",
        "/internal/deployments/42/rollback/",
        "/internal/deployments/41/rollback/",
    ]


@pytest.mark.asyncio
async def test_a_rollback_conflict_is_a_typed_issue() -> None:
    rec = Recorder(lambda _r: httpx.Response(409, json={}))
    with pytest.raises(CMSValidationError) as caught:
        await provider(rec).rollback(
            deployment_id="41", operation_id=uuid.uuid4(), actor="abhinav.kumar"
        )
    assert [i.code for i in caught.value.issues] == ["rollback_conflict"]


@pytest.mark.asyncio
async def test_an_unknown_deployment_is_not_found() -> None:
    rec = Recorder(lambda _r: httpx.Response(404))
    with pytest.raises(CMSNotFoundError):
        await provider(rec).rollback(
            deployment_id="41", operation_id=uuid.uuid4(), actor="abhinav.kumar"
        )


# ---------------------------------------------------------------- health
@pytest.mark.asyncio
async def test_health_reports_the_gate_and_never_reaches_the_cms() -> None:
    """A health check must not be a way to reach the production CMS without passing the gate."""
    rec = Recorder()
    health = await provider(rec).health()
    assert health.reachable is True
    assert health.detail["writes_allowed"] is True
    assert health.detail["read_key_set"] is True
    assert rec.requests == [], "health opened a connection"


@pytest.mark.asyncio
async def test_health_names_the_refusal_when_writes_are_disabled() -> None:
    health = await provider(Recorder(), writes_enabled=False).health()
    assert health.reachable is True
    assert health.detail["writes_allowed"] is False
    assert "KEN_CMS_WRITES_ENABLED" in health.detail["writes_refused"]


@pytest.mark.asyncio
async def test_health_is_unreachable_rather_than_raising_when_unconfigured() -> None:
    health = await provider(Recorder(), base_url=None).health()
    assert health.reachable is False
    assert health.error is not None
