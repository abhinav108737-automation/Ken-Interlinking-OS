"""Service-account authentication for GA4 / Search Console. Google is never called: every request
goes to an ``httpx.MockTransport`` that verifies the signed assertion with the PUBLIC key."""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ken_core.providers.analytics.base import AnalyticsFetchError, FetchErrorCode
from ken_core.providers.analytics.ga4 import Ga4AnalyticsProvider
from ken_core.providers.analytics.gsc import SearchConsoleAnalyticsProvider
from ken_core.providers.analytics.service_account import (
    GA4_SCOPE,
    GSC_SCOPE,
    TOKEN_URI,
    ServiceAccountError,
    ServiceAccountTokenSource,
    load_key,
)
from tests.conftest import make_settings
from tests.unit.test_analytics_google import PAGES, RANGE, SITE_PROPERTY, _resolver

EMAIL = "pilot-reader@ken-project.iam.gserviceaccount.com"
ACCESS = "ya29.minted-ACCESS-token"
PRIVATE_MARKER = "BEGIN PRIVATE KEY"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = _KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()


def _key_file(tmp_path: Path, *, mode: int = 0o600, **changes: Any) -> Path:
    body: dict[str, Any] = {
        "type": "service_account",
        "project_id": "ken-project",
        "private_key_id": "abc123",
        "private_key": PEM,
        "client_email": EMAIL,
        "client_id": "1",
        "token_uri": TOKEN_URI,
    }
    body.update(changes)
    path = tmp_path / "sa.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    path.chmod(mode)
    return path


def _unb64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


class Google:
    """Fake Google: a token endpoint that verifies the assertion, and canned API answers."""

    def __init__(self, *, token_status: int = 200, expires_in: int = 3600) -> None:
        self.token_status = token_status
        self.expires_in = expires_in
        self.requests: list[httpx.Request] = []
        self.assertions: list[dict[str, Any]] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            form = dict(part.split("=", 1) for part in request.content.decode().split("&"))
            assert form["grant_type"].startswith("urn%3Aietf")
            head, claims, signature = form["assertion"].split(".")
            _KEY.public_key().verify(
                _unb64(signature),
                f"{head}.{claims}".encode(),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            self.assertions.append(
                {"header": json.loads(_unb64(head)), "claims": json.loads(_unb64(claims))}
            )
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "x"})
            return httpx.Response(200, json={"access_token": ACCESS, "expires_in": self.expires_in})
        if request.url.host == "analyticsdata.googleapis.com":
            return httpx.Response(200, json={"rows": [], "rowCount": 0})
        return httpx.Response(200, json={"rows": []})

    def api_calls(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host != "oauth2.googleapis.com"]


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


async def test_the_assertion_is_a_valid_rs256_jwt_with_read_only_scope(tmp_path: Path) -> None:
    google = Google()
    source = ServiceAccountTokenSource(
        str(_key_file(tmp_path)), transport=google.transport(), clock=Clock()
    )
    assert await source.token(GA4_SCOPE) == ACCESS
    sent = google.assertions[0]
    assert sent["header"] == {"alg": "RS256", "typ": "JWT"}
    claims = sent["claims"]
    assert claims["iss"] == EMAIL and claims["aud"] == TOKEN_URI and claims["scope"] == GA4_SCOPE
    assert claims["exp"] - claims["iat"] == 3600
    assert str(google.requests[0].url) == TOKEN_URI
    await source.aclose()


async def test_tokens_are_cached_per_scope_and_refreshed_before_expiry(tmp_path: Path) -> None:
    google, clock = Google(expires_in=3600), Clock()
    source = ServiceAccountTokenSource(
        str(_key_file(tmp_path)), transport=google.transport(), clock=clock
    )
    await source.token(GA4_SCOPE)
    await source.token(GA4_SCOPE)
    assert len(google.assertions) == 1, "the second call is served from the cache"
    await source.token(GSC_SCOPE)
    assert len(google.assertions) == 2 and google.assertions[1]["claims"]["scope"] == GSC_SCOPE
    clock.now += 3600 - 60  # inside the refresh margin
    await source.token(GA4_SCOPE)
    assert len(google.assertions) == 3
    await source.aclose()


async def test_only_the_two_read_only_scopes_can_be_requested(tmp_path: Path) -> None:
    source = ServiceAccountTokenSource(
        str(_key_file(tmp_path)), transport=Google().transport(), clock=Clock()
    )
    with pytest.raises(ServiceAccountError):
        await source.token("https://www.googleapis.com/auth/analytics.edit")
    await source.aclose()


async def test_the_token_uri_in_the_file_is_ignored(tmp_path: Path) -> None:
    google = Google()
    source = ServiceAccountTokenSource(
        str(_key_file(tmp_path, token_uri="https://evil.example.test/token")),
        transport=google.transport(),
        clock=Clock(),
    )
    await source.token(GA4_SCOPE)
    assert {r.url.host for r in google.requests} == {"oauth2.googleapis.com"}
    await source.aclose()


@pytest.mark.parametrize(
    "build",
    [
        lambda p: _key_file(p, mode=0o644),
        lambda p: _key_file(p, type="authorized_user"),
        lambda p: _key_file(p, client_email="someone@example.com"),
        lambda p: _key_file(p, private_key="not a pem"),
        lambda p: _key_file(
            p, private_key="-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"
        ),
        lambda p: p / "missing.json",
    ],
)
def test_a_bad_key_file_is_refused_with_a_fixed_code(tmp_path: Path, build: Any) -> None:
    with pytest.raises(ServiceAccountError) as exc:
        load_key(str(build(tmp_path)))
    assert exc.value.code == "key_invalid" and PRIVATE_MARKER not in str(exc.value)


def test_a_key_that_is_not_json_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "x.json"
    path.write_text("{not json", encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(ServiceAccountError):
        load_key(str(path))


async def test_google_refusing_the_assertion_is_google_auth_failed(tmp_path: Path) -> None:
    source = ServiceAccountTokenSource(
        str(_key_file(tmp_path)), transport=Google(token_status=400).transport(), clock=Clock()
    )
    with pytest.raises(ServiceAccountError) as exc:
        await source.token(GA4_SCOPE)
    assert exc.value.code == "auth_failed"
    await source.aclose()


async def test_a_flaky_token_endpoint_is_retried_a_bounded_number_of_times(tmp_path: Path) -> None:
    google = Google(token_status=503)
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    source = ServiceAccountTokenSource(
        str(_key_file(tmp_path)), transport=google.transport(), clock=Clock(), sleep=sleep
    )
    with pytest.raises(ServiceAccountError) as exc:
        await source.token(GA4_SCOPE)
    assert exc.value.code == "unavailable" and len(google.assertions) == 3 and len(slept) == 2
    await source.aclose()


async def test_ga4_uses_the_service_account_for_its_bearer_token(tmp_path: Path) -> None:
    google = Google()
    settings = make_settings(
        environment="local",
        analytics_google_enabled=True,
        analytics_service_account_file=str(_key_file(tmp_path)),
        ga4_property_id="456789",
    )
    provider = Ga4AnalyticsProvider(settings, resolver=_resolver(), transport=google.transport())
    await provider.fetch_batch(date_range=RANGE, datasets=PAGES)
    call = google.api_calls()[0]
    assert call.headers["authorization"] == f"Bearer {ACCESS}"
    assert google.assertions[0]["claims"]["scope"] == GA4_SCOPE
    assert PRIVATE_MARKER not in call.content.decode() and EMAIL not in call.content.decode()
    await provider.aclose()


async def test_search_console_uses_the_webmasters_scope(tmp_path: Path) -> None:
    google = Google()
    settings = make_settings(
        environment="local",
        analytics_google_enabled=True,
        analytics_service_account_file=str(_key_file(tmp_path)),
        gsc_site_url=SITE_PROPERTY,
    )
    provider = SearchConsoleAnalyticsProvider(
        settings, resolver=_resolver(), transport=google.transport()
    )
    await provider.fetch_batch(date_range=RANGE, datasets=PAGES)
    assert google.assertions[0]["claims"]["scope"] == GSC_SCOPE
    assert google.api_calls()[0].headers["authorization"] == f"Bearer {ACCESS}"
    await provider.aclose()


async def test_disabled_egress_refuses_before_the_key_is_touched(tmp_path: Path) -> None:
    google = Google()
    settings = make_settings(
        environment="local",
        analytics_google_enabled=False,
        analytics_service_account_file=str(tmp_path / "does-not-exist.json"),
        ga4_property_id="456789",
    )
    provider = Ga4AnalyticsProvider(settings, resolver=_resolver(), transport=google.transport())
    with pytest.raises(AnalyticsFetchError) as exc:
        await provider.fetch_batch(date_range=RANGE, datasets=PAGES)
    assert exc.value.code is FetchErrorCode.GOOGLE_DISABLED and google.requests == []


async def test_a_bad_key_surfaces_as_token_file_invalid_without_any_google_call(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    google = Google()
    settings = make_settings(
        environment="local",
        analytics_google_enabled=True,
        analytics_service_account_file=str(_key_file(tmp_path, mode=0o644)),
        ga4_property_id="456789",
    )
    provider = Ga4AnalyticsProvider(settings, resolver=_resolver(), transport=google.transport())
    with caplog.at_level(logging.DEBUG), pytest.raises(AnalyticsFetchError) as exc:
        await provider.fetch_batch(date_range=RANGE, datasets=PAGES)
    assert exc.value.code is FetchErrorCode.TOKEN_FILE_INVALID and google.requests == []
    assert PRIVATE_MARKER not in caplog.text and PEM[40:80] not in caplog.text


def test_settings_accept_one_credential_source_and_refuse_both(tmp_path: Path) -> None:
    key = str(_key_file(tmp_path))
    token = tmp_path / "t"
    token.write_text("abc\n")
    ok = make_settings(
        environment="local",
        analytics_google_enabled=True,
        analytics_service_account_file=key,
        ga4_property_id="1",
    )
    assert ok.analytics_service_account_file == key
    assert "analytics_service_account_file" not in ok.public_settings()
    with pytest.raises(Exception, match="only one"):
        make_settings(
            environment="local",
            analytics_google_enabled=True,
            analytics_service_account_file=key,
            analytics_google_token_file=str(token),
        )
    with pytest.raises(Exception, match="requires"):
        make_settings(environment="local", analytics_google_enabled=True)
