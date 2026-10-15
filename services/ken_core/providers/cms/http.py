"""``HttpCMSProvider``: the production adapter for TRD section 13 (ADR-0014 s4 and s8).

Endpoints (``{id}`` is URL-path-escaped, ``.`` / ``..`` included, so an id can never
leave its endpoint's path)::

    GET  /internal/pages/{id}                         -> CMSPage
    POST /internal/pages/{id}/link-changes/validate   -> ValidationResult
    POST /internal/pages/{id}/link-changes            -> DeploymentResult   (409: version conflict)
    POST /internal/deployments/{id}/rollback          -> RollbackResult     (409: rollback conflict)

Request bodies are the contract models' JSON; responses are validated through the same
models (Pydantic at the service boundary). Status mapping: 404 -> :class:`CMSNotFoundError`;
409 on link-changes -> :class:`CMSVersionConflictError` (``expected`` / ``current`` from the
body when present); 409 on rollback -> :class:`CMSValidationError` ``rollback_conflict``;
400/422 -> :class:`CMSValidationError` with the body's ``issues``; 3xx (redirects are
never followed), 401/403, 5xx, timeouts, network errors, malformed bodies and OFF-CONTRACT
bodies -> :class:`CMSUnavailableError` (an UNKNOWN outcome for a write: the deployment stays
``applying`` and reconciliation resends the stored request). Off-contract includes a body
that validates but answers a different request: a page with another ``page_id``, a
deployment result for another ``operation_id`` / ``source_page_id``, a dry run reported as
anything but ``no_op``, a rollback result for another ``deployment_id``.

**Gating -- checked before ANY I/O, reads included:**

* the base URL must be set and be ``http(s)`` with a valid port and no credentials; it must
  be loopback unless
  ``KEN_CMS_ALLOW_REMOTE=true`` (connecting to the real Ken CMS is an ASK action and is not
  done here);
* writes (apply, rollback) additionally need ``KEN_CMS_WRITES_ENABLED=true`` and a token;
* the Bearer token comes from ``KEN_CMS_TOKEN`` (a ``SecretStr``); it is never logged, never
  put in an exception message, and never sent anywhere but the configured base URL
  (``follow_redirects=False``, ``trust_env=False``: no proxy or netrc).

Rollback: this adapter calls the TRD rollback endpoint; a CMS that cannot roll back by
deployment id is driven by the deployment service with ``LinkOperation.REMOVE`` changes
(per-plan rollback) instead.
"""

from __future__ import annotations

import ipaddress
import json
from typing import Any, Final
from urllib.parse import quote, urlsplit

import httpx
from pydantic import SecretStr, ValidationError

from ken_contracts.cms import (
    CMSNotFoundError,
    CMSPage,
    CMSUnavailableError,
    CMSValidationError,
    CMSVersionConflictError,
    DeploymentResult,
    LinkChangeRequest,
    RollbackResult,
    ValidationIssue,
    ValidationResult,
)
from ken_contracts.common import OperationId
from ken_contracts.model import ProviderHealth
from ken_core.config import ConfigurationError

__all__ = ["PROVIDER_NAME", "CMSAccessRefusedError", "HttpCMSProvider"]

PROVIDER_NAME: Final = "http"
_LOOPBACK_NAMES: Final = frozenset({"localhost"})
_MAX_ISSUES: Final = 50


class CMSAccessRefusedError(CMSUnavailableError):
    """The gating refused the call before any I/O (not configured, remote, writes off)."""


def _is_loopback(host: str) -> bool:
    name = host.strip("[]").rstrip(".").lower()
    if name in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


class HttpCMSProvider:
    """:class:`~ken_contracts.cms.CMSProvider` over HTTP (module docstring)."""

    name: str = PROVIDER_NAME
    preserves_data_attributes: bool | None = None
    """Unknown until observed against the real CMS (ADR-0014 s8: attribution must not
    depend on ``data-ken-link-id``)."""

    def __init__(
        self,
        *,
        base_url: str | None,
        token: SecretStr | None,
        writes_enabled: bool = False,
        allow_remote: bool = False,
        timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = (base_url or "").rstrip("/")
        self._token = token
        self._writes = writes_enabled
        self._allow_remote = allow_remote
        self._timeout = timeout_s
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._closed = False

    # --------------------------------------------------------------- gating
    def _gate(self, *, write: bool) -> None:
        if self._closed:
            raise CMSAccessRefusedError("HttpCMSProvider has been closed")
        if not self._base:
            raise CMSAccessRefusedError("KEN_CMS_BASE_URL is not set")
        try:
            parts = urlsplit(self._base)
        except ValueError:
            raise CMSAccessRefusedError("KEN_CMS_BASE_URL is invalid") from None
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise CMSAccessRefusedError("KEN_CMS_BASE_URL must be an http(s) URL")
        try:
            parts.port  # noqa: B018 - raises ValueError on an invalid port
        except ValueError:
            raise CMSAccessRefusedError("KEN_CMS_BASE_URL has an invalid port") from None
        if parts.username or parts.password:
            raise CMSAccessRefusedError("KEN_CMS_BASE_URL must not carry credentials")
        if not _is_loopback(parts.hostname) and not self._allow_remote:
            raise CMSAccessRefusedError(
                "KEN_CMS_BASE_URL is not loopback; set KEN_CMS_ALLOW_REMOTE=true only with "
                "explicit approval (connecting to a real CMS is an ASK action)"
            )
        if write:
            if not self._writes:
                raise CMSAccessRefusedError("CMS writes are disabled (KEN_CMS_WRITES_ENABLED)")
            if self._token is None or not self._token.get_secret_value():
                raise CMSAccessRefusedError("KEN_CMS_TOKEN is not set")

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base,
                timeout=self._timeout,
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            )
        return self._client

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._token is not None and self._token.get_secret_value():
            headers["Authorization"] = "Bearer " + self._token.get_secret_value()
        return headers

    async def _call(
        self, method: str, path: str, *, write: bool, body: str | None = None
    ) -> httpx.Response:
        self._gate(write=write)
        headers = self._headers()
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = await self._http().request(method, path, content=body, headers=headers)
        except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError, OSError) as exc:
            # The class name only: an httpx message can include the URL.
            raise CMSUnavailableError(f"CMS request failed ({type(exc).__name__})") from None
        if response.status_code >= 500 or 300 <= response.status_code < 400:
            raise CMSUnavailableError(f"CMS answered HTTP {response.status_code}")
        if response.status_code in {401, 403}:
            raise CMSUnavailableError(f"CMS refused the credentials (HTTP {response.status_code})")
        return response

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            raise CMSUnavailableError("CMS answered a body that is not JSON") from None

    @classmethod
    def _issues(cls, response: httpx.Response) -> list[ValidationIssue]:
        try:
            data = response.json()
        except ValueError:
            data = None
        raw = data.get("issues") if isinstance(data, dict) else None
        issues: list[ValidationIssue] = []
        for item in raw if isinstance(raw, list) else []:
            try:
                issues.append(ValidationIssue.model_validate(item))
            except ValidationError:
                continue
            if len(issues) >= _MAX_ISSUES:
                break
        if not issues:
            issues.append(
                ValidationIssue(
                    code="cms_validation_failed",
                    severity="error",
                    message=f"The CMS refused the request (HTTP {response.status_code}).",
                )
            )
        return issues

    @staticmethod
    def _model[M: (CMSPage, ValidationResult, DeploymentResult, RollbackResult)](
        model: type[M], response: httpx.Response
    ) -> M:
        try:
            return model.model_validate_json(response.content)
        except ValidationError:
            raise CMSUnavailableError("CMS answered a body outside the contract") from None

    @staticmethod
    def _segment(value: str) -> str:
        """One path segment. ``quote`` leaves ``.`` alone and httpx normalises dot segments,
        so ``..`` would otherwise climb out of the endpoint's path."""
        escaped = quote(value, safe="")
        return escaped.replace(".", "%2E") if escaped in {".", ".."} else escaped

    @classmethod
    def _page_path(cls, page_id: str) -> str:
        return "/internal/pages/" + cls._segment(page_id)

    @staticmethod
    def _off_contract() -> CMSUnavailableError:
        return CMSUnavailableError("CMS answered a body outside the contract")

    # ------------------------------------------------------------- protocol
    async def get_page(self, *, page_id: str) -> CMSPage:
        response = await self._call("GET", self._page_path(page_id), write=False)
        if response.status_code == 404:
            raise CMSNotFoundError("no such page")
        if response.status_code != 200:
            raise CMSUnavailableError(f"CMS answered HTTP {response.status_code}")
        page = self._model(CMSPage, response)
        if page.page_id != page_id:
            raise self._off_contract()
        return page

    async def validate_link_changes(self, *, request: LinkChangeRequest) -> ValidationResult:
        path = self._page_path(request.source_page_id) + "/link-changes/validate"
        response = await self._call("POST", path, write=False, body=request.model_dump_json())
        if response.status_code == 404:
            raise CMSNotFoundError("no such page")
        if response.status_code in {400, 409, 422}:
            return ValidationResult(valid=False, issues=self._issues(response))
        if response.status_code != 200:
            raise CMSUnavailableError(f"CMS answered HTTP {response.status_code}")
        return self._model(ValidationResult, response)

    async def apply_link_changes(self, *, request: LinkChangeRequest) -> DeploymentResult:
        path = self._page_path(request.source_page_id) + "/link-changes"
        # A dry run is a read; a real apply is a write (gated).
        response = await self._call(
            "POST", path, write=not request.dry_run, body=request.model_dump_json()
        )
        if response.status_code == 404:
            raise CMSNotFoundError("no such page")
        if response.status_code == 409:
            data = self._json(response) if response.content else {}
            current = data.get("current") if isinstance(data, dict) else None
            raise CMSVersionConflictError(
                "Page version has changed; regenerate from current content.",
                expected=request.expected_version,
                current=str(current) if isinstance(current, str) else "unknown",
            )
        if response.status_code in {400, 422}:
            raise CMSValidationError("The CMS rejected the change", issues=self._issues(response))
        if response.status_code not in {200, 201}:
            raise CMSUnavailableError(f"CMS answered HTTP {response.status_code}")
        result = self._model(DeploymentResult, response)
        if (
            result.operation_id != request.operation_id
            or result.source_page_id != request.source_page_id
            or (request.dry_run and result.status != "no_op")
        ):
            raise self._off_contract()
        return result

    async def rollback(
        self, *, deployment_id: str, operation_id: OperationId, actor: str
    ) -> RollbackResult:
        path = "/internal/deployments/" + self._segment(deployment_id) + "/rollback"
        body = json.dumps({"operation_id": str(operation_id), "actor": actor})
        response = await self._call("POST", path, write=True, body=body)
        if response.status_code == 404:
            raise CMSNotFoundError("no such deployment")
        if response.status_code == 409:
            issues = self._issues(response)
            if not any(i.code == "rollback_conflict" for i in issues):
                issues.insert(
                    0,
                    ValidationIssue(
                        code="rollback_conflict",
                        severity="error",
                        message="The CMS refused the rollback: our link changed.",
                    ),
                )
            raise CMSValidationError("Rollback rejected", issues=issues)
        if response.status_code in {400, 422}:
            raise CMSValidationError("Rollback rejected", issues=self._issues(response))
        if response.status_code not in {200, 201}:
            raise CMSUnavailableError(f"CMS answered HTTP {response.status_code}")
        result = self._model(RollbackResult, response)
        if result.deployment_id != deployment_id:
            raise self._off_contract()
        return result

    async def health(self) -> ProviderHealth:
        """The GATING state only -- never any I/O (``reachable`` means "calls are allowed")."""
        try:
            self._gate(write=False)
        except CMSAccessRefusedError as exc:
            return ProviderHealth(provider=PROVIDER_NAME, reachable=False, error=str(exc))
        try:
            self._gate(write=True)
            writes_refused: str | None = None
        except CMSAccessRefusedError as exc:
            writes_refused = str(exc)
        return ProviderHealth(
            provider=PROVIDER_NAME,
            reachable=True,
            detail={
                "probe": "configuration only",
                "writes_enabled": self._writes,
                "writes_allowed": writes_refused is None,
                "writes_refused": writes_refused,
                "remote_allowed": self._allow_remote,
            },
        )

    async def aclose(self) -> None:
        self._closed = True
        if self._client is not None:
            client, self._client = self._client, None
            await client.aclose()


def validate_settings_shape(base_url: str | None) -> None:
    """Startup-time sanity (no I/O): a set base URL must be http(s) without credentials."""
    if base_url is None:
        return
    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"} or parts.username or parts.password:
        raise ConfigurationError("KEN_CMS_BASE_URL must be an http(s) URL without credentials")
