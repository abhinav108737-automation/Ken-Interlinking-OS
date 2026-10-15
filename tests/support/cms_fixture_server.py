"""A local TRD section 13 CMS fixture server, for ``HttpCMSProvider`` contract tests.

The four endpoints delegate to a backing :class:`~ken_core.providers.cms.mock.MockCMSProvider`
(so the HTTP adapter is tested against the SAME behaviour as the in-memory provider), with
the contract models' JSON as bodies and the status mapping documented in
``ken_core/providers/cms/http.py``:

* ``CMSNotFoundError`` -> 404;
* ``CMSVersionConflictError`` -> 409 ``{"expected": ..., "current": ...}``;
* ``CMSValidationError`` -> 422 ``{"issues": [...]}``, except on rollback, where an issue
  ``rollback_conflict`` is a 409 ``{"issues": [...]}``;
* ``CMSUnavailableError`` -> 503; a body that is not the contract model -> 400 with issues;
* a request without ``Authorization: Bearer <token>`` -> 401.

Bound to ``127.0.0.1`` on port 0 (an ephemeral port: never a fixed port, never 8765). After
start-up the bound socket is read back and ``GET /__fixture__/identity`` must return this
instance's random nonce, so a test can only ever talk to the listener it started.

    async with CMSFixtureServer(backend, token="t") as server:
        HttpCMSProvider(base_url=server.base_url, token=SecretStr("t"), writes_enabled=True)
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Final
from uuid import UUID

import httpx
import uvicorn
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ken_contracts.cms import (
    CMSError,
    CMSNotFoundError,
    CMSUnavailableError,
    CMSValidationError,
    CMSVersionConflictError,
    LinkChangeRequest,
)
from ken_core.providers.cms.mock import MockCMSProvider

__all__ = ["CMSFixtureServer", "LoggedCall"]

HOST: Final = "127.0.0.1"
FORBIDDEN_PORTS: Final = frozenset({0, 5432, 8765})
_ROLLBACK_CONFLICT: Final = "rollback_conflict"


class _RollbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    actor: str


@dataclass(frozen=True, slots=True)
class LoggedCall:
    method: str
    path: str
    authorization: str | None
    body: bytes


def _issues_body(exc: CMSValidationError) -> dict[str, Any]:
    return {"issues": [i.model_dump(mode="json") for i in exc.issues]}


def _bad_request(message: str) -> JSONResponse:
    issue = {"code": "invalid_request", "severity": "error", "message": message}
    return JSONResponse({"issues": [issue]}, status_code=400)


class CMSFixtureServer:
    """Async context manager running the fixture app under uvicorn in the current loop."""

    def __init__(self, backend: MockCMSProvider, *, token: str) -> None:
        self.backend = backend
        self.token = token
        self.calls: list[LoggedCall] = []
        self._nonce = secrets.token_hex(16)
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._port: int | None = None
        self.app = Starlette(
            routes=[
                Route("/__fixture__/identity", self._identity, methods=["GET"]),
                Route("/internal/pages/{page_id}", self._get_page, methods=["GET"]),
                Route(
                    "/internal/pages/{page_id}/link-changes/validate",
                    self._validate,
                    methods=["POST"],
                ),
                Route("/internal/pages/{page_id}/link-changes", self._apply, methods=["POST"]),
                Route(
                    "/internal/deployments/{deployment_id}/rollback",
                    self._rollback,
                    methods=["POST"],
                ),
            ]
        )

    # ------------------------------------------------------------- addressing
    @property
    def port(self) -> int:
        assert self._port is not None, "server not started"
        return self._port

    @property
    def base_url(self) -> str:
        return f"http://{HOST}:{self.port}"

    # -------------------------------------------------------------- lifecycle
    async def __aenter__(self) -> CMSFixtureServer:
        config = uvicorn.Config(
            self.app,
            host=HOST,
            port=0,
            # Never touch the test process's logging: with log_config=None, a log_level or
            # access_log=False would still rewrite the process-wide uvicorn.* loggers
            # (levels, handlers, propagation) and break tests/unit/test_logging.py.
            log_config=None,
            log_level=None,
            lifespan="off",
        )
        server = uvicorn.Server(config)
        self._server = server
        self._task = asyncio.create_task(server.serve())
        for _ in range(500):
            if server.started or self._task.done():
                break
            await asyncio.sleep(0.01)
        if not server.started:
            await self._stop()
            raise RuntimeError("the CMS fixture server did not start")
        sockets = [sock for srv in server.servers for sock in (srv.sockets or ())]
        if len(sockets) != 1:
            await self._stop()
            raise RuntimeError("the CMS fixture server bound an unexpected number of sockets")
        host, port = sockets[0].getsockname()[:2]
        if host != HOST or port in FORBIDDEN_PORTS:
            await self._stop()
            raise RuntimeError("the CMS fixture server bound an unexpected address")
        self._port = int(port)
        await self._verify_identity()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._stop()

    async def _stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            finally:
                self._task = None

    async def _verify_identity(self) -> None:
        """The listener on our port must be THIS server (not some other local process)."""
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            response = await client.get(f"{self.base_url}/__fixture__/identity", timeout=5)
        if response.status_code != 200 or response.json().get("nonce") != self._nonce:
            await self._stop()
            raise RuntimeError("the listener on the fixture port is not this fixture server")

    # --------------------------------------------------------------- handlers
    async def _log(self, request: Request) -> bytes:
        body = await request.body()
        self.calls.append(
            LoggedCall(
                method=request.method,
                path=request.url.path,
                authorization=request.headers.get("authorization"),
                body=body,
            )
        )
        return body

    def _authorised(self, request: Request) -> bool:
        return request.headers.get("authorization") == f"Bearer {self.token}"

    async def _identity(self, request: Request) -> Response:
        return JSONResponse({"nonce": self._nonce})

    async def _get_page(self, request: Request) -> Response:
        await self._log(request)
        if not self._authorised(request):
            return JSONResponse({"detail": "unauthorised"}, status_code=401)
        try:
            page = await self.backend.get_page(page_id=request.path_params["page_id"])
        except CMSError as exc:
            return self._error(exc, rollback=False)
        return Response(page.model_dump_json(), media_type="application/json")

    async def _change_request(self, request: Request) -> LinkChangeRequest | Response:
        body = await self._log(request)
        if not self._authorised(request):
            return JSONResponse({"detail": "unauthorised"}, status_code=401)
        try:
            parsed = LinkChangeRequest.model_validate_json(body)
        except ValidationError:
            return _bad_request("The body is not a LinkChangeRequest.")
        if parsed.source_page_id != request.path_params["page_id"]:
            return _bad_request("source_page_id does not match the path.")
        return parsed

    async def _validate(self, request: Request) -> Response:
        parsed = await self._change_request(request)
        if isinstance(parsed, Response):
            return parsed
        try:
            result = await self.backend.validate_link_changes(request=parsed)
        except CMSError as exc:
            return self._error(exc, rollback=False)
        return Response(result.model_dump_json(), media_type="application/json")

    async def _apply(self, request: Request) -> Response:
        parsed = await self._change_request(request)
        if isinstance(parsed, Response):
            return parsed
        try:
            result = await self.backend.apply_link_changes(request=parsed)
        except CMSError as exc:
            return self._error(exc, rollback=False)
        return Response(result.model_dump_json(), media_type="application/json")

    async def _rollback(self, request: Request) -> Response:
        body = await self._log(request)
        if not self._authorised(request):
            return JSONResponse({"detail": "unauthorised"}, status_code=401)
        try:
            parsed = _RollbackBody.model_validate_json(body)
        except ValidationError:
            return _bad_request("The body is not a rollback request.")
        try:
            result = await self.backend.rollback(
                deployment_id=request.path_params["deployment_id"],
                operation_id=parsed.operation_id,
                actor=parsed.actor,
            )
        except CMSError as exc:
            return self._error(exc, rollback=True)
        return Response(result.model_dump_json(), media_type="application/json")

    @staticmethod
    def _error(exc: CMSError, *, rollback: bool) -> Response:
        if isinstance(exc, CMSNotFoundError):
            return JSONResponse({"detail": "not found"}, status_code=404)
        if isinstance(exc, CMSVersionConflictError):
            return JSONResponse({"expected": exc.expected, "current": exc.current}, status_code=409)
        if isinstance(exc, CMSValidationError):
            conflict = rollback and any(i.code == _ROLLBACK_CONFLICT for i in exc.issues)
            return JSONResponse(_issues_body(exc), status_code=409 if conflict else 422)
        if isinstance(exc, CMSUnavailableError):
            return JSONResponse({"detail": "unavailable"}, status_code=503)
        return JSONResponse({"detail": "error"}, status_code=500)
