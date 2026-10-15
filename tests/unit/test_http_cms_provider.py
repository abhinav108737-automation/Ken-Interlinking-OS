"""HttpCMSProvider: gating before ANY I/O, the token's confinement, and the status mapping.

Pure unit tests over an ``httpx.MockTransport`` that RECORDS every request; no socket is ever
opened (the end-to-end contract runs against the local fixture server in
tests/integration/test_cms_provider_contract.py).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Final

import httpx
import pytest
import structlog
from pydantic import SecretStr

from ken_contracts.cms import (
    CMSError,
    CMSNotFoundError,
    CMSPage,
    CMSUnavailableError,
    CMSValidationError,
    CMSVersionConflictError,
    DeploymentResult,
    LinkChange,
    LinkChangeRequest,
    LinkLocator,
    LinkOperation,
    RollbackResult,
    ValidationResult,
)
from ken_core.providers.cms.http import CMSAccessRefusedError, HttpCMSProvider

TOKEN: Final = "tok-SECRET-91b7e2d4"
BASE: Final = "http://127.0.0.1:18080"
PAGE: Final = "page-1"
DEPLOYMENT: Final = "dep-1"
NOW: Final = datetime(2026, 10, 15, tzinfo=UTC)

type Handler = Callable[[httpx.Request], httpx.Response]


class Recorder:
    """A transport handler that records each request, then answers via ``handler``."""

    def __init__(self, handler: Handler | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.handler: Handler = handler or (lambda _r: httpx.Response(500))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


def provider(
    recorder: Recorder,
    *,
    base_url: str | None = BASE,
    token: str | None = TOKEN,
    writes_enabled: bool = True,
    allow_remote: bool = False,
) -> HttpCMSProvider:
    return HttpCMSProvider(
        base_url=base_url,
        token=None if token is None else SecretStr(token),
        writes_enabled=writes_enabled,
        allow_remote=allow_remote,
        transport=httpx.MockTransport(recorder),
    )


def change_request(*, dry_run: bool = False, source: str = PAGE) -> LinkChangeRequest:
    return LinkChangeRequest(
        operation_id=uuid.uuid4(),
        source_page_id=source,
        expected_version="v1",
        changes=[
            LinkChange(
                change_id=uuid.uuid4(),
                operation=LinkOperation.INSERT,
                target_page_id="page-2",
                href="https://example.com/page-2",
                anchor_text="anchor",
                locator=LinkLocator(block_selector="b0", context_hash="0" * 64),
            )
        ],
        dry_run=dry_run,
        actor="user:operator",
    )


def page_json(page_id: str = PAGE) -> dict[str, Any]:
    return CMSPage(
        page_id=page_id, url="https://example.com/p", title="t", body_html="<p>x</p>", version="v1"
    ).model_dump(mode="json")


def deployment_json(req: LinkChangeRequest, **overrides: Any) -> dict[str, Any]:
    result = DeploymentResult(
        deployment_id=DEPLOYMENT,
        operation_id=req.operation_id,
        source_page_id=req.source_page_id,
        status="no_op" if req.dry_run else "applied",
        applied_change_ids=[] if req.dry_run else [c.change_id for c in req.changes],
        previous_version="v1",
        new_version="v1" if req.dry_run else "v2",
        applied_at=NOW,
    ).model_dump(mode="json")
    return result | overrides


def rollback_json(deployment_id: str = DEPLOYMENT) -> dict[str, Any]:
    return RollbackResult(
        deployment_id=deployment_id,
        status="rolled_back",
        restored_version="v3",
        rolled_back_at=NOW,
        actor="user:operator",
    ).model_dump(mode="json")


def json_response(status: int, body: Any) -> Handler:
    return lambda _r: httpx.Response(status, json=body)


Op = Callable[[HttpCMSProvider], Awaitable[Any]]
READS: Final[dict[str, Op]] = {
    "get_page": lambda p: p.get_page(page_id=PAGE),
    "validate": lambda p: p.validate_link_changes(request=change_request()),
    "dry_run_apply": lambda p: p.apply_link_changes(request=change_request(dry_run=True)),
}
WRITES: Final[dict[str, Op]] = {
    "apply": lambda p: p.apply_link_changes(request=change_request()),
    "rollback": lambda p: p.rollback(
        deployment_id=DEPLOYMENT, operation_id=uuid.uuid4(), actor="user:operator"
    ),
}
ALL_OPS: Final = READS | WRITES


def assert_token_absent(exc: BaseException) -> None:
    seen: BaseException | None = exc
    while seen is not None:
        for rendering in (str(seen), repr(seen), repr(seen.args)):
            assert TOKEN not in rendering
        for issue in getattr(seen, "issues", []):
            assert TOKEN not in issue.model_dump_json()
        seen = seen.__cause__ or seen.__context__


# --------------------------------------------------------------------------- #
# Gating: refused BEFORE any I/O
# --------------------------------------------------------------------------- #
BAD_BASES: Final = {
    "unset": None,
    "empty": "",
    "not_http": "ftp://127.0.0.1:21",
    "no_scheme": "127.0.0.1:8080",
    "file": "file:///etc/passwd",
    "credentials": "http://admin:pw-Zq81@127.0.0.1:18080",
    "remote_name": "https://cms.example.com",
    "remote_ip": "http://10.0.0.5:80",
    "unspecified": "http://0.0.0.0:18080",
    "lookalike": "http://localhost.example.com",
    "loopback_userinfo_trick": "http://127.0.0.1@cms.example.com",
    "bad_port": "http://127.0.0.1:notaport",
}


@pytest.mark.parametrize("op_name", sorted(ALL_OPS))
@pytest.mark.parametrize("base_name", sorted(BAD_BASES))
async def test_every_call_is_refused_before_io_for_a_bad_base_url(
    base_name: str, op_name: str
) -> None:
    recorder = Recorder()
    cms = provider(recorder, base_url=BAD_BASES[base_name])
    with pytest.raises(CMSAccessRefusedError) as exc:
        await ALL_OPS[op_name](cms)
    assert recorder.requests == []
    assert isinstance(exc.value, CMSUnavailableError)  # the protocol's error family
    assert "pw-Zq81" not in str(exc.value)
    assert_token_absent(exc.value)
    await cms.aclose()


@pytest.mark.parametrize("op_name", sorted(ALL_OPS))
async def test_a_remote_base_url_is_allowed_only_when_opted_in(op_name: str) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/rollback"):
            return httpx.Response(200, json=rollback_json())
        if request.url.path.endswith("/validate"):
            return httpx.Response(200, json=ValidationResult(valid=True).model_dump(mode="json"))
        if request.method == "POST":
            return httpx.Response(
                200, json=deployment_json(LinkChangeRequest.model_validate_json(request.content))
            )
        return httpx.Response(200, json=page_json())

    refused = Recorder(answer)
    cms = provider(refused, base_url="https://cms.example.com")
    with pytest.raises(CMSAccessRefusedError):
        await ALL_OPS[op_name](cms)
    assert refused.requests == []

    allowed = Recorder(answer)
    remote = provider(allowed, base_url="https://cms.example.com", allow_remote=True)
    await ALL_OPS[op_name](remote)
    assert [r.url.host for r in allowed.requests] == ["cms.example.com"]
    await remote.aclose()


@pytest.mark.parametrize("op_name", sorted(WRITES))
@pytest.mark.parametrize(
    ("writes_enabled", "token"),
    [(False, TOKEN), (True, None), (True, ""), (False, None)],
)
async def test_writes_need_the_flag_and_a_token(
    op_name: str, writes_enabled: bool, token: str | None
) -> None:
    recorder = Recorder()
    cms = provider(recorder, writes_enabled=writes_enabled, token=token)
    with pytest.raises(CMSAccessRefusedError):
        await WRITES[op_name](cms)
    assert recorder.requests == []


@pytest.mark.parametrize("op_name", sorted(READS))
async def test_reads_and_dry_runs_need_neither_writes_nor_a_token(op_name: str) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/validate"):
            return httpx.Response(200, json=ValidationResult(valid=True).model_dump(mode="json"))
        if request.method == "POST":
            return httpx.Response(
                200, json=deployment_json(LinkChangeRequest.model_validate_json(request.content))
            )
        return httpx.Response(200, json=page_json())

    recorder = Recorder(answer)
    cms = provider(recorder, writes_enabled=False, token=None)
    await READS[op_name](cms)
    assert len(recorder.requests) == 1
    assert "authorization" not in recorder.requests[0].headers


async def test_a_closed_provider_refuses_before_io() -> None:
    recorder = Recorder(json_response(200, page_json()))
    cms = provider(recorder)
    await cms.aclose()
    with pytest.raises(CMSAccessRefusedError):
        await cms.get_page(page_id=PAGE)
    assert recorder.requests == []


# --------------------------------------------------------------------------- #
# Health: never any I/O
# --------------------------------------------------------------------------- #
async def test_health_reports_the_gating_without_io() -> None:
    recorder = Recorder()
    unset = await provider(recorder, base_url=None).health()
    assert unset.reachable is False
    assert unset.error is not None

    remote = await provider(recorder, base_url="https://cms.example.com").health()
    assert remote.reachable is False

    read_only = await provider(recorder, writes_enabled=False).health()
    assert read_only.reachable is True
    assert read_only.detail["writes_allowed"] is False
    assert read_only.detail["writes_refused"]

    no_token = await provider(recorder, token=None).health()
    assert no_token.detail["writes_allowed"] is False

    ready = await provider(recorder).health()
    assert ready.reachable is True
    assert ready.detail["writes_allowed"] is True
    assert ready.detail["remote_allowed"] is False
    for health in (unset, remote, read_only, no_token, ready):
        assert TOKEN not in health.model_dump_json()
    assert recorder.requests == []


# --------------------------------------------------------------------------- #
# The token: Bearer, to the base URL only, never logged
# --------------------------------------------------------------------------- #
async def test_the_token_is_sent_only_as_bearer_to_the_base_url() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/rollback"):
            return httpx.Response(200, json=rollback_json())
        if request.url.path.endswith("/validate"):
            return httpx.Response(200, json=ValidationResult(valid=True).model_dump(mode="json"))
        if request.method == "POST":
            return httpx.Response(
                200, json=deployment_json(LinkChangeRequest.model_validate_json(request.content))
            )
        return httpx.Response(200, json=page_json())

    recorder = Recorder(answer)
    cms = provider(recorder)
    for op in ALL_OPS.values():
        await op(cms)
    assert len(recorder.requests) == len(ALL_OPS)
    for request in recorder.requests:
        assert request.url.scheme == "http"
        assert (request.url.host, request.url.port) == ("127.0.0.1", 18080)
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in str(request.url)
        assert TOKEN.encode() not in request.content
    await cms.aclose()


async def test_the_token_never_reaches_a_log_or_an_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    statuses = iter([302, 500, 404, 409, 422, 401, 400, 200])

    def answer(_request: httpx.Request) -> httpx.Response:
        status = next(statuses, 503)
        body: Any = {"issues": [{"code": "x", "severity": "error", "message": "m"}]}
        headers = {"Location": "http://evil.example/steal"} if status == 302 else {}
        return httpx.Response(status, json=body if status != 200 else {"bogus": 1}, headers=headers)

    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    recorder = Recorder(answer)
    cms = provider(recorder)
    errors: list[BaseException] = []
    with structlog.testing.capture_logs() as captured:
        for _ in range(3):
            for op in ALL_OPS.values():
                try:
                    await op(cms)
                except CMSError as error:
                    errors.append(error)
        recorder.handler = boom
        for op in ALL_OPS.values():
            with pytest.raises(CMSUnavailableError) as raised:
                await op(cms)
            errors.append(raised.value)
    assert errors
    for seen in errors:
        assert_token_absent(seen)
    assert TOKEN not in caplog.text
    assert all(TOKEN not in record.getMessage() for record in caplog.records)
    assert TOKEN not in repr(captured)
    assert TOKEN not in repr(cms)
    assert TOKEN not in repr(vars(cms))
    await cms.aclose()


# --------------------------------------------------------------------------- #
# Transport failures and off-contract answers -> CMSUnavailableError (unknown outcome)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("op_name", sorted(ALL_OPS))
@pytest.mark.parametrize("status", [301, 302, 307, 308])
async def test_redirects_are_never_followed(op_name: str, status: int) -> None:
    recorder = Recorder(
        lambda _r: httpx.Response(status, headers={"Location": "http://evil.example/x"})
    )
    cms = provider(recorder)
    with pytest.raises(CMSUnavailableError):
        await ALL_OPS[op_name](cms)
    assert len(recorder.requests) == 1
    assert recorder.requests[0].url.host == "127.0.0.1"


@pytest.mark.parametrize("op_name", sorted(ALL_OPS))
@pytest.mark.parametrize("status", [500, 502, 503, 504, 401, 403, 418])
async def test_server_errors_are_an_unknown_outcome(op_name: str, status: int) -> None:
    cms = provider(Recorder(json_response(status, {"detail": "x"})))
    with pytest.raises(CMSUnavailableError):
        await ALL_OPS[op_name](cms)


@pytest.mark.parametrize("op_name", sorted(ALL_OPS))
@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        httpx.ConnectTimeout("slow"),
        httpx.RemoteProtocolError("garbage"),
    ],
    ids=type,
)
async def test_network_errors_and_timeouts_are_an_unknown_outcome(
    op_name: str, error: Exception
) -> None:
    def fail(_request: httpx.Request) -> httpx.Response:
        raise error

    cms = provider(Recorder(fail))
    with pytest.raises(CMSUnavailableError) as exc:
        await ALL_OPS[op_name](cms)
    assert "127.0.0.1" not in str(exc.value)  # the class name only, never the URL


@pytest.mark.parametrize("op_name", sorted(ALL_OPS))
@pytest.mark.parametrize(
    "body",
    [b"not json", b"{", b"[]", b'{"bogus": 1}', b"null"],
    ids=["text", "truncated", "list", "wrong_shape", "null"],
)
async def test_malformed_or_off_contract_bodies_are_an_unknown_outcome(
    op_name: str, body: bytes
) -> None:
    cms = provider(Recorder(lambda _r: httpx.Response(200, content=body)))
    with pytest.raises(CMSUnavailableError):
        await ALL_OPS[op_name](cms)


async def test_a_page_for_another_id_is_off_contract() -> None:
    cms = provider(Recorder(json_response(200, page_json("someone-else"))))
    with pytest.raises(CMSUnavailableError):
        await cms.get_page(page_id=PAGE)


@pytest.mark.parametrize(
    "field",
    ["operation_id", "source_page_id", "dry_run_applied"],
)
async def test_a_deployment_result_for_another_request_is_off_contract(field: str) -> None:
    dry = field == "dry_run_applied"
    req = change_request(dry_run=dry)
    overrides: dict[str, Any] = {
        "operation_id": {"operation_id": str(uuid.uuid4())},
        "source_page_id": {"source_page_id": "other-page"},
        "dry_run_applied": {"status": "applied", "new_version": "v2"},
    }[field]
    cms = provider(Recorder(json_response(200, deployment_json(req, **overrides))))
    with pytest.raises(CMSUnavailableError):
        await cms.apply_link_changes(request=req)


async def test_a_rollback_result_for_another_deployment_is_off_contract() -> None:
    cms = provider(Recorder(json_response(200, rollback_json("dep-other"))))
    with pytest.raises(CMSUnavailableError):
        await cms.rollback(deployment_id=DEPLOYMENT, operation_id=uuid.uuid4(), actor="user:x")


# --------------------------------------------------------------------------- #
# Definitive answers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("op_name", sorted(ALL_OPS))
async def test_404_is_not_found(op_name: str) -> None:
    cms = provider(Recorder(json_response(404, {"detail": "nope"})))
    with pytest.raises(CMSNotFoundError):
        await ALL_OPS[op_name](cms)


@pytest.mark.parametrize("dry_run", [False, True])
async def test_409_on_link_changes_is_a_version_conflict(dry_run: bool) -> None:
    req = change_request(dry_run=dry_run)
    cms = provider(Recorder(json_response(409, {"expected": "v1", "current": "v9"})))
    with pytest.raises(CMSVersionConflictError) as exc:
        await cms.apply_link_changes(request=req)
    assert (exc.value.expected, exc.value.current) == ("v1", "v9")

    bare = provider(Recorder(lambda _r: httpx.Response(409)))
    with pytest.raises(CMSVersionConflictError) as exc:
        await bare.apply_link_changes(request=req)
    assert exc.value.current == "unknown"


@pytest.mark.parametrize(
    "body",
    [
        {"issues": [{"code": "rollback_conflict", "severity": "error", "message": "changed"}]},
        {"issues": [{"code": "something_else", "severity": "error", "message": "m"}]},
        {},
    ],
    ids=["with_code", "other_code", "empty"],
)
async def test_409_on_rollback_is_a_rollback_conflict(body: dict[str, Any]) -> None:
    cms = provider(Recorder(json_response(409, body)))
    with pytest.raises(CMSValidationError) as exc:
        await cms.rollback(deployment_id=DEPLOYMENT, operation_id=uuid.uuid4(), actor="user:x")
    found = [i.code for i in exc.value.issues]
    assert "rollback_conflict" in found
    assert found.count("rollback_conflict") == 1


@pytest.mark.parametrize("status", [400, 422])
@pytest.mark.parametrize("op_name", ["apply", "dry_run_apply", "rollback"])
async def test_400_and_422_carry_the_body_issues(op_name: str, status: int) -> None:
    body = {
        "issues": [
            {"code": "operation_id_reused", "severity": "error", "message": "m"},
            {"code": "not-an-issue"},  # off-contract items are dropped
        ]
    }
    cms = provider(Recorder(json_response(status, body)))
    with pytest.raises(CMSValidationError) as exc:
        await ALL_OPS[op_name](cms)
    assert [i.code for i in exc.value.issues] == ["operation_id_reused"]

    empty = provider(Recorder(lambda _r: httpx.Response(status, content=b"oops")))
    with pytest.raises(CMSValidationError) as exc:
        await ALL_OPS[op_name](empty)
    assert [i.code for i in exc.value.issues] == ["cms_validation_failed"]


@pytest.mark.parametrize("status", [400, 409, 422])
async def test_validate_refusals_are_an_invalid_result(status: int) -> None:
    body = {"issues": [{"code": "block_not_found", "severity": "error", "message": "m"}]}
    cms = provider(Recorder(json_response(status, body)))
    result = await cms.validate_link_changes(request=change_request())
    assert result.valid is False
    assert [i.code for i in result.issues] == ["block_not_found"]


# --------------------------------------------------------------------------- #
# Requests on the wire
# --------------------------------------------------------------------------- #
async def test_requests_use_the_trd_endpoints_and_contract_bodies() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/rollback"):
            return httpx.Response(200, json=rollback_json())
        if request.url.path.endswith("/validate"):
            return httpx.Response(200, json=ValidationResult(valid=True).model_dump(mode="json"))
        if request.method == "POST":
            return httpx.Response(
                201, json=deployment_json(LinkChangeRequest.model_validate_json(request.content))
            )
        return httpx.Response(200, json=page_json())

    recorder = Recorder(answer)
    cms = provider(recorder)
    req = change_request()
    operation_id = uuid.uuid4()
    await cms.get_page(page_id=PAGE)
    await cms.validate_link_changes(request=req)
    result = await cms.apply_link_changes(request=req)
    await cms.rollback(deployment_id=DEPLOYMENT, operation_id=operation_id, actor="user:x")
    assert [(r.method, r.url.path) for r in recorder.requests] == [
        ("GET", f"/internal/pages/{PAGE}"),
        ("POST", f"/internal/pages/{PAGE}/link-changes/validate"),
        ("POST", f"/internal/pages/{PAGE}/link-changes"),
        ("POST", f"/internal/deployments/{DEPLOYMENT}/rollback"),
    ]
    assert LinkChangeRequest.model_validate_json(recorder.requests[1].content) == req
    assert LinkChangeRequest.model_validate_json(recorder.requests[2].content) == req
    assert result.operation_id == req.operation_id
    rollback_body = httpx.Response(200, content=recorder.requests[3].content).json()
    assert rollback_body == {"operation_id": str(operation_id), "actor": "user:x"}
    for request in recorder.requests[1:]:
        assert request.headers["content-type"] == "application/json"


@pytest.mark.parametrize(
    ("page_id", "raw_path"),
    [
        ("..", b"/internal/pages/%2E%2E"),
        (".", b"/internal/pages/%2E"),
        ("a/b", b"/internal/pages/a%2Fb"),
        ("x?y#z", b"/internal/pages/x%3Fy%23z"),
    ],
)
async def test_ids_never_leave_their_path_segment(page_id: str, raw_path: bytes) -> None:
    recorder = Recorder(json_response(200, page_json(page_id)))
    cms = provider(recorder)
    await cms.get_page(page_id=page_id)
    assert recorder.requests[0].url.raw_path == raw_path
    assert recorder.requests[0].url.host == "127.0.0.1"


async def test_a_base_url_path_prefix_is_kept() -> None:
    recorder = Recorder(json_response(200, page_json()))
    cms = provider(recorder, base_url="http://127.0.0.1:18080/cms/")
    await cms.get_page(page_id=PAGE)
    assert recorder.requests[0].url.path == f"/cms/internal/pages/{PAGE}"


def test_preserves_data_attributes_is_unknown() -> None:
    assert HttpCMSProvider.preserves_data_attributes is None
