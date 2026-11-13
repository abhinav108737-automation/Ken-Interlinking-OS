"""``KenConsultingCMSProvider``: the adapter for the real CMS (ADR-0028).

The technology team's API is a near neighbour of the contract in
``docs/handover/CMS_INTEGRATION_REQUIREMENTS.md``, not the contract itself. Everything that
differs is translated HERE, so that plans, locators, the deployment service and the review
flow stay exactly as they are. Endpoints, relative to ``KEN_CMS_BASE_URL`` (which is their
base MINUS the trailing ``/internal``)::

    GET  /internal/pages/{slug}:{field}/              -> the field: version + html
    GET  /internal/reports/{slug}/                    -> the report: protected, title, related
    POST /internal/link-changes/validate/             -> {"changes": [...]}, writes nothing
    POST /internal/pages/{slug}:{field}/link-changes/ -> ONE link; {"status", "deployment_id"}
    POST /internal/deployments/{id}/rollback/         -> {"status": "rolled_back"}

**Every path ends in a slash.** Django answers a slash-less path with a 301 and this client
never follows redirects, so the slash is not cosmetic: without it every call fails.

Three translations are worth knowing before reading the code:

* **One call per change.** Their apply endpoint carries a single link; our request carries one
  or more. They are applied in order, the field's ``version`` is re-read between them (the
  first write invalidates the lock the second holds), and a failure part-way rolls back what
  was already applied, in reverse, before raising. ``deployment_id`` is then the per-change
  ids joined by ``,`` and :meth:`rollback` unwinds them in reverse.
* **Occurrence.** ``LinkLocator.occurrence_index`` is read as the 0-based occurrence of the
  anchor WITHIN THE FIELD, and sent as ``occurrence = index + 1``. Working that index out from
  a page-level plan is phase 2 (ADR-0028) and is not done here.
* **href.** Sent site-relative. An absolute URL on a host we recognise is reduced to its path;
  any other absolute URL is refused rather than sent.

Gating is the same as :class:`~ken_core.providers.cms.http.HttpCMSProvider`: base URL checked
before any I/O, loopback unless ``KEN_CMS_ALLOW_REMOTE``, writes additionally needing
``KEN_CMS_WRITES_ENABLED`` and the write key. Two keys, not one: reads send
``KEN_CMS_READ_TOKEN``, writes send ``KEN_CMS_WRITE_TOKEN``. Neither is ever logged, put in an
exception message, or sent anywhere but the configured base URL.

A write is NEVER retried automatically. Until the team answers what a replayed
``operation_id`` does (ADR-0028 open question 1), a timeout is an unknown outcome and the
deployment service's reconciliation — which reads the live page — is the only safe resolution.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import quote, urlsplit

import httpx
from pydantic import SecretStr

from ken_contracts.cms import (
    CMSNotFoundError,
    CMSPage,
    CMSUnavailableError,
    CMSValidationError,
    CMSVersionConflictError,
    DeploymentResult,
    LinkChange,
    LinkChangeRequest,
    RollbackResult,
    ValidationIssue,
    ValidationResult,
)
from ken_contracts.common import OperationId
from ken_contracts.model import ProviderHealth
from ken_core.logging import get_logger
from ken_core.providers.cms.http import CMSAccessRefusedError, _is_loopback

__all__ = ["PROVIDER_NAME", "KenConsultingCMSProvider"]

logger = get_logger(__name__)

PROVIDER_NAME: Final = "kenconsulting"
_MAX_ISSUES: Final = 50
_DEPLOYMENT_SEPARATOR: Final = ","

#: Hosts whose absolute URLs may be reduced to a site-relative path. Anything else is refused:
#: this system is not permitted to insert a link that leaves the site.
SITE_HOSTS: Final = frozenset(
    {
        "www.kenresearch.com",
        "kenresearch.com",
        "www.thekenconsulting.com",
        "thekenconsulting.com",
    }
)

#: The keys their field payload might carry the HTML under. Their guide greps the response for
#: ``<a href=...>``, so the HTML is in the body, but does not name the key. The first present
#: key wins; none present is off-contract and fails loudly rather than writing to a guess.
_HTML_KEYS: Final = ("body_html", "html", "content", "text", "value")

#: Warning codes their validate endpoint names in the guide. Anything else they report is kept
#: verbatim, so a code we have never seen is surfaced to the reviewer rather than swallowed.
_KNOWN_WARNINGS: Final = frozenset({"self_link", "already_linked"})


class KenConsultingCMSProvider:
    """:class:`~ken_contracts.cms.CMSProvider` over the Ken Consulting API (ADR-0028)."""

    name: str = PROVIDER_NAME
    preserves_data_attributes: bool | None = None
    """Unknown until observed. Open question 5: does saving reformat the HTML?"""

    def __init__(
        self,
        *,
        base_url: str | None,
        read_token: SecretStr | None,
        write_token: SecretStr | None,
        writes_enabled: bool = False,
        allow_remote: bool = False,
        timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = (base_url or "").rstrip("/")
        self._read_token = read_token
        self._write_token = write_token
        self._writes = writes_enabled
        self._allow_remote = allow_remote
        self._timeout = timeout_s
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._closed = False

    # --------------------------------------------------------------- gating
    def _gate(self, *, write: bool, write_key: bool = False) -> None:
        """``write`` is a real mutation; ``write_key`` is their validate endpoint, which
        changes nothing but is authorised with the write key. Validate therefore needs the key
        to exist WITHOUT needing writes to be enabled — that is the whole point of a dry run."""
        if self._closed:
            raise CMSAccessRefusedError("KenConsultingCMSProvider has been closed")
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
        if write and not self._writes:
            raise CMSAccessRefusedError("CMS writes are disabled (KEN_CMS_WRITES_ENABLED)")
        if write or write_key:
            if self._write_token is None or not self._write_token.get_secret_value():
                raise CMSAccessRefusedError("KEN_CMS_WRITE_TOKEN is not set")
        elif self._read_token is None or not self._read_token.get_secret_value():
            raise CMSAccessRefusedError("KEN_CMS_READ_TOKEN is not set")

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

    def _headers(self, *, use_write_key: bool) -> dict[str, str]:
        """Their validate endpoint writes nothing but is authorised with the WRITE key."""
        token = self._write_token if use_write_key else self._read_token
        headers = {"Accept": "application/json"}
        if token is not None and token.get_secret_value():
            headers["Authorization"] = "Bearer " + token.get_secret_value()
        return headers

    async def _call(
        self,
        method: str,
        path: str,
        *,
        write: bool,
        write_key: bool = False,
        body: str | None = None,
    ) -> httpx.Response:
        """``write`` gates as a mutation; ``write_key`` only selects the key (validate)."""
        self._gate(write=write, write_key=write_key)
        headers = self._headers(use_write_key=write or write_key)
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = await self._http().request(method, path, content=body, headers=headers)
        except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError, OSError) as exc:
            # The class name only: an httpx message can include the URL.
            raise CMSUnavailableError(f"CMS request failed ({type(exc).__name__})") from None
        if 300 <= response.status_code < 400:
            # Almost certainly APPEND_SLASH. Named, because the cause is not obvious.
            raise CMSUnavailableError(
                f"CMS answered a redirect (HTTP {response.status_code}); redirects are never "
                "followed. Check the trailing slash on the endpoint path."
            )
        if response.status_code >= 500:
            raise CMSUnavailableError(f"CMS answered HTTP {response.status_code}")
        if response.status_code in {401, 403}:
            raise CMSUnavailableError(f"CMS refused the credentials (HTTP {response.status_code})")
        return response

    # ----------------------------------------------------------- translation
    @staticmethod
    def _segment(value: str) -> str:
        """One path segment. ``:`` is safe — it is their page-id separator, and escaping it
        would address a page that does not exist."""
        escaped = quote(value, safe=":")
        return escaped.replace(".", "%2E") if escaped in {".", ".."} else escaped

    @classmethod
    def _page_path(cls, page_id: str) -> str:
        return "/internal/pages/" + cls._segment(page_id) + "/"

    @staticmethod
    def _report_slug(page_id: str) -> str:
        """``slug:field`` -> ``slug``. A page id without a field is its own slug."""
        return page_id.split(":", 1)[0]

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            raise CMSUnavailableError("CMS answered a body that is not JSON") from None

    @staticmethod
    def _relative_href(href: str) -> str:
        """Site-relative, as their API expects. Refuses anything that leaves the site."""
        if href.startswith("/"):
            return href
        parts = urlsplit(href)
        if parts.scheme in {"http", "https"} and (parts.hostname or "").lower() in SITE_HOSTS:
            return parts.path + (f"?{parts.query}" if parts.query else "")
        raise CMSValidationError(
            "The link target is not a site-relative path",
            issues=[
                ValidationIssue(
                    code="href_not_site_relative",
                    severity="error",
                    message="Only a path on this site may be inserted as a link.",
                )
            ],
        )

    @classmethod
    def _link(cls, change: LinkChange) -> dict[str, Any]:
        """One ``LinkChange`` as their ``link`` object. ``occurrence`` is 1-based."""
        return {
            "anchor_text": change.anchor_text,
            "href": cls._relative_href(change.href),
            "occurrence": change.locator.occurrence_index + 1,
        }

    @classmethod
    def _issues(cls, response: httpx.Response) -> list[ValidationIssue]:
        """Their refusals as our issues. Their guide names ``self_link`` and ``already_linked``
        as warnings and ``anchor_not_found`` / ``page_protected`` as 422 codes; any other code
        is kept verbatim at error severity rather than dropped."""
        try:
            data = response.json()
        except ValueError:
            data = None
        issues: list[ValidationIssue] = []
        if isinstance(data, dict):
            for key in ("issues", "warnings", "errors"):
                for item in data.get(key) or []:
                    issues.append(cls._issue(item, warning=key == "warnings"))
                    if len(issues) >= _MAX_ISSUES:
                        return issues
            code = data.get("code") or data.get("detail")
            if not issues and isinstance(code, str):
                issues.append(cls._issue(code, warning=False))
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
    def _issue(item: Any, *, warning: bool) -> ValidationIssue:
        if isinstance(item, str):
            code, message = item, item
        elif isinstance(item, dict):
            code = str(item.get("code") or item.get("type") or "cms_validation_failed")
            message = str(item.get("message") or item.get("detail") or code)
        else:  # pragma: no cover - defensive
            code = message = "cms_validation_failed"
        severity = "warning" if warning and code in _KNOWN_WARNINGS else "error"
        return ValidationIssue(code=code, severity=severity, message=message)

    # ------------------------------------------------------------- protocol
    async def get_page(self, *, page_id: str) -> CMSPage:
        """One field, in ONE call.

        VERIFIED against the live API 2026-11-13: ``GET /pages/<slug>:<field>/`` answers
        ``page_id``, ``url``, ``title``, ``body_html``, ``version`` and ``protected`` together,
        so the report does not need a second read. ``protected`` being on the field response is
        what makes that safe -- a page whose protection we could not read is not editable.
        """
        response = await self._call("GET", self._page_path(page_id), write=False)
        if response.status_code == 404:
            raise CMSNotFoundError("no such page")
        if response.status_code != 200:
            raise CMSUnavailableError(f"CMS answered HTTP {response.status_code}")
        field = self._json(response)
        if not isinstance(field, dict):
            raise CMSUnavailableError("CMS answered a body outside the contract")
        version = field.get("version")
        html = next((field[k] for k in _HTML_KEYS if isinstance(field.get(k), str)), None)
        if not isinstance(version, str) or html is None:
            raise CMSUnavailableError(
                "CMS field payload carries no 'version' or no recognised HTML key"
            )

        if "protected" not in field:
            raise CMSUnavailableError("CMS field payload carries no 'protected' flag")
        slug = self._report_slug(page_id)
        return CMSPage(
            page_id=str(field.get("page_id") or page_id),
            url=str(field.get("url") or f"/{slug}"),
            title=str(field.get("title") or slug),
            body_html=html,
            version=version,
            protected=bool(field["protected"]),
            metadata={"slug": slug, "field": page_id.partition(":")[2]},
        )

    async def _report(self, slug: str) -> dict[str, Any]:
        response = await self._call(
            "GET", "/internal/reports/" + self._segment(slug) + "/", write=False
        )
        if response.status_code == 404:
            raise CMSNotFoundError("no such report")
        if response.status_code != 200:
            raise CMSUnavailableError(f"CMS answered HTTP {response.status_code}")
        data = self._json(response)
        if not isinstance(data, dict) or "protected" not in data:
            raise CMSUnavailableError("CMS report payload carries no 'protected' flag")
        return data

    async def validate_link_changes(self, *, request: LinkChangeRequest) -> ValidationResult:
        """Their collection endpoint, every change in one call. Writes nothing; needs the
        write key."""
        body = json.dumps(
            {
                "changes": [
                    {
                        "page_id": request.source_page_id,
                        "expected_version": request.expected_version,
                        "link": self._link(change),
                    }
                    for change in request.changes
                ]
            }
        )
        response = await self._call(
            "POST", "/internal/link-changes/validate/", write=False, write_key=True, body=body
        )
        if response.status_code == 404:
            raise CMSNotFoundError("no such page")
        if response.status_code in {400, 409, 422}:
            return ValidationResult(valid=False, issues=self._issues(response))
        if response.status_code != 200:
            raise CMSUnavailableError(f"CMS answered HTTP {response.status_code}")
        return self._validation(self._json(response), request)

    @staticmethod
    def _validation(data: Any, request: LinkChangeRequest) -> ValidationResult:
        """Their batch-validate body, VERIFIED against the live API 2026-11-13::

            {"total": 6, "valid": 5, "invalid": 1,
             "results": [{"index": 0, "page_id": "...", "valid": true},
                         {"index": 1, ..., "valid": false,
                          "error": "anchor_not_found",
                          "message": "anchor_text occurrence 1 not found in a linkable
                                      text node"}]}

        ``valid`` at the top level is a COUNT, not a boolean -- reading it as one is how this
        adapter would have failed on its first real call. The verdict is ``invalid == 0``, and
        each failed result is mapped back to its ``change_id`` through ``index``, so the
        reviewer sees which of their links was refused rather than a page-level "no".
        """
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise CMSUnavailableError("CMS answered a body outside the contract")
        issues: list[ValidationIssue] = []
        for item in data["results"]:
            if not isinstance(item, dict) or item.get("valid"):
                continue
            index = item.get("index")
            change_id = (
                request.changes[index].change_id
                if isinstance(index, int) and 0 <= index < len(request.changes)
                else None
            )
            code = str(item.get("error") or "cms_validation_failed")
            issues.append(
                ValidationIssue(
                    code=code,
                    severity="warning" if code in _KNOWN_WARNINGS else "error",
                    message=str(item.get("message") or code),
                    change_id=change_id,
                )
            )
        invalid = data.get("invalid")
        valid = invalid == 0 if isinstance(invalid, int) else not issues
        return ValidationResult(valid=valid, issues=issues)

    async def apply_link_changes(self, *, request: LinkChangeRequest) -> DeploymentResult:
        """One call per change, in order, compensating on failure (ADR-0028 §3).

        A dry run never reaches their apply endpoint: it is their validate endpoint, reported as
        the ``no_op`` our contract requires.
        """
        if request.dry_run:
            result = await self.validate_link_changes(request=request)
            if not result.valid:
                raise CMSValidationError("The CMS refused the change", issues=result.issues)
            return self._result(request, deployment_ids=[], version=request.expected_version)

        applied: list[str] = []
        version = request.expected_version
        previous_version = request.expected_version
        for change in request.changes:
            try:
                deployment_id, version = await self._apply_one(request, change, version=version)
            except Exception:
                await self._compensate(applied)
                raise
            applied.append(deployment_id)
        return self._result(
            request, deployment_ids=applied, version=version, previous=previous_version
        )

    async def _apply_one(
        self, request: LinkChangeRequest, change: LinkChange, *, version: str
    ) -> tuple[str, str]:
        """Apply one link. Returns its deployment id and the field's version afterwards."""
        body = json.dumps(
            {
                "operation_id": f"{request.operation_id}:{change.change_id}",
                "expected_version": version,
                "link": self._link(change),
            }
        )
        path = self._page_path(request.source_page_id) + "link-changes/"
        response = await self._call("POST", path, write=True, body=body)
        if response.status_code == 404:
            raise CMSNotFoundError("no such page")
        if response.status_code == 409:
            data = self._json(response) if response.content else {}
            current = data.get("current") or data.get("current_version")
            raise CMSVersionConflictError(
                "Page version has changed; regenerate from current content.",
                expected=version,
                current=str(current) if isinstance(current, str) else "unknown",
            )
        if response.status_code in {400, 422}:
            raise CMSValidationError("The CMS rejected the change", issues=self._issues(response))
        if response.status_code not in {200, 201}:
            raise CMSUnavailableError(f"CMS answered HTTP {response.status_code}")
        data = self._json(response)
        # A replay answers ``"replayed": true`` (Snehal, 2026-11-13) and is a SUCCESS: the link
        # is already there and nothing was inserted twice. Treating it as off-contract would
        # turn the safe outcome of a retry into a failure.
        if not isinstance(data, dict) or not (
            data.get("status") == "applied" or data.get("replayed") is True
        ):
            raise CMSUnavailableError("CMS answered a body outside the contract")
        deployment_id = data.get("deployment_id")
        if deployment_id is None:
            raise CMSUnavailableError("CMS applied a change without returning a deployment_id")
        # Their payload may or may not carry the new version; re-read when it does not, because
        # the next change in this request needs a lock that is not already stale.
        new_version = data.get("new_version") or data.get("version")
        if not isinstance(new_version, str):
            new_version = (await self.get_page(page_id=request.source_page_id)).version
        return str(deployment_id), new_version

    async def _compensate(self, applied: list[str]) -> None:
        """Unwind what this request already applied, in reverse. Best effort by nature: if the
        unwind fails the field is part-changed, which is reported as an unknown write."""
        for deployment_id in reversed(applied):
            try:
                await self._rollback_one(deployment_id)
            except Exception as exc:
                # Logged, never swallowed: this is the one path that can leave a field with
                # some of a deployment's links in it. The class name only, never the message.
                logger.error(
                    "cms_compensating_rollback_failed",
                    deployment_id=deployment_id,
                    error=type(exc).__name__,
                )

    def _result(
        self,
        request: LinkChangeRequest,
        *,
        deployment_ids: list[str],
        version: str,
        previous: str | None = None,
    ) -> DeploymentResult:
        return DeploymentResult(
            deployment_id=_DEPLOYMENT_SEPARATOR.join(deployment_ids),
            operation_id=request.operation_id,
            source_page_id=request.source_page_id,
            status="no_op" if request.dry_run else "applied",
            applied_change_ids=[] if request.dry_run else [c.change_id for c in request.changes],
            previous_version=previous or request.expected_version,
            new_version=version,
            applied_at=datetime.now(UTC),
        )

    async def _rollback_one(self, deployment_id: str) -> None:
        path = "/internal/deployments/" + self._segment(deployment_id) + "/rollback/"
        response = await self._call("POST", path, write=True)
        if response.status_code == 404:
            raise CMSNotFoundError("no such deployment")
        if response.status_code == 409:
            raise CMSValidationError(
                "The CMS refused the rollback",
                issues=[
                    ValidationIssue(
                        code="rollback_conflict",
                        severity="error",
                        message="The page changed since this deployment; nothing was restored.",
                    )
                ],
            )
        if response.status_code not in {200, 201}:
            raise CMSUnavailableError(f"CMS answered HTTP {response.status_code}")
        data = self._json(response)
        if not isinstance(data, dict) or data.get("status") != "rolled_back":
            raise CMSUnavailableError("CMS answered a body outside the contract")

    async def rollback(
        self, *, deployment_id: str, operation_id: OperationId, actor: str
    ) -> RollbackResult:
        """Unwind a composite deployment in reverse. ``actor`` is recorded on our side: their
        payload has nowhere to put it (ADR-0028 §6)."""
        ids = [i for i in deployment_id.split(_DEPLOYMENT_SEPARATOR) if i]
        if not ids:
            raise CMSNotFoundError("no such deployment")
        for one in reversed(ids):
            await self._rollback_one(one)
        return RollbackResult(
            deployment_id=deployment_id,
            status="rolled_back",
            restored_version="unknown",
            rolled_back_at=datetime.now(UTC),
            actor=actor,
        )

    async def health(self) -> ProviderHealth:
        """The GATING state only -- never any I/O, as in :class:`HttpCMSProvider`. A health
        check must not be a way to reach the production CMS without passing the gate."""
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
                "read_key_set": self._read_token is not None,
                "write_key_set": self._write_token is not None,
            },
        )

    async def aclose(self) -> None:
        self._closed = True
        if self._client is not None:
            await self._client.aclose()
            self._client = None
