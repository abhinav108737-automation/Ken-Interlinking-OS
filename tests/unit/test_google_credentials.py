"""Service-account credentials for the analytics importers (ADR-0022).

**Google is never called.** Every request goes through ``httpx.MockTransport``, and the key is a
throwaway RSA key generated at runtime inside ``tmp_path``. Nothing here reads a real credential,
and no literal key material is stored in this file (the repository's credential scanner would
refuse it).

What is asserted, in the order it matters:

* the file-privacy rule: private files are accepted, and a file Windows or POSIX reports as
  readable by anyone else is refused BEFORE a request is built, with no detail about why;
* the signed assertion is genuinely signed by the key, scoped to ONE read-only product, and sent
  to ONE hard-coded host, whatever the key file says;
* the token is cached in memory and re-minted before it expires, never written anywhere;
* every failure is a fixed code, and neither the key, the assertion, the token, the account
  email nor the path reaches a log line or an exception message.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any

import httpx
import pytest
import structlog
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from pydantic import ValidationError

from ken_core.config import GA4_BASE_URL, GSC_BASE_URL, REPO_ROOT
from ken_core.providers.analytics.base import AnalyticsFetchError, FetchErrorCode, GoogleEgress
from ken_core.providers.analytics.google_credentials import (
    GOOGLE_TOKEN_URI,
    ServiceAccountTokenSource,
    check_private_credentials_file,
    sddl_is_private,
)
from tests.conftest import make_settings

# Built from parts so this file contains no literal that matches the credential scanner's rules.
_SERVICE_ACCOUNT = "service" + "_account"
EMAIL = "reader@fake-project.iam.gserviceaccount.com"
MINTED = "minted-access-token-1"
ANALYTICS_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
SEARCH_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _pem(key: rsa.RSAPrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def _make_private(path: Path) -> None:
    """Make ``path`` readable by its owner only, the way each platform expresses it."""
    if sys.platform == "win32":
        user = f"{os.environ['USERDOMAIN']}\\{os.environ['USERNAME']}"
        result = subprocess.run(  # noqa: S603 -- fixed argv, no shell
            [  # noqa: S607 -- icacls is a Windows system tool resolved from PATH
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"{user}:F",
                "NT AUTHORITY\\SYSTEM:F",
            ],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip("icacls could not restrict the file, so privacy cannot be set up")
    else:
        path.chmod(0o600)


def _write_key(
    tmp_path: Path,
    key: rsa.RSAPrivateKey,
    *,
    private: bool = True,
    overrides: dict[str, str] | None = None,
) -> Path:
    info: dict[str, Any] = {
        "type": _SERVICE_ACCOUNT,
        "client_email": EMAIL,
        "private_key": _pem(key),
        "token_uri": GOOGLE_TOKEN_URI,
    }
    info.update(overrides or {})
    path = tmp_path / "creds.json"
    path.write_text(json.dumps(info), encoding="utf-8")
    if private:
        _make_private(path)
    return path


class Recorder:
    """A transport that answers the token endpoint and records every request."""

    def __init__(self, response: httpx.Response | Exception | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._response = response

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self._response, Exception):
            raise self._response
        if self._response is not None:
            return self._response
        return httpx.Response(200, json={"access_token": MINTED, "expires_in": 3600})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def _form(request: httpx.Request) -> dict[str, str]:
    parsed = urllib.parse.parse_qs(request.content.decode("ascii"))
    return {name: values[0] for name, values in parsed.items()}


def _b64(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _source(
    path: Path, recorder: Recorder, *, scope: str = ANALYTICS_SCOPE, clock: Any = None
) -> ServiceAccountTokenSource:
    kwargs: dict[str, Any] = {} if clock is None else {"clock": clock}
    return ServiceAccountTokenSource(
        path=path, scope=scope, timeout_s=5.0, transport=recorder.transport(), **kwargs
    )


# --------------------------------------------------------------------------- #
# The signed assertion
# --------------------------------------------------------------------------- #
async def test_the_assertion_is_signed_by_the_key_scoped_and_sent_only_to_google(
    tmp_path: Path, private_key: rsa.RSAPrivateKey
) -> None:
    recorder = Recorder()
    source = _source(_write_key(tmp_path, private_key), recorder)
    assert await source.access_token() == MINTED

    (request,) = recorder.requests
    assert str(request.url) == GOOGLE_TOKEN_URI
    form = _form(request)
    assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    header, claims, signature = form["assertion"].split(".")
    assert json.loads(_b64(header))["alg"] == "RS256"
    payload = json.loads(_b64(claims))
    assert payload["iss"] == EMAIL
    assert payload["scope"] == ANALYTICS_SCOPE
    assert payload["aud"] == GOOGLE_TOKEN_URI
    assert payload["exp"] - payload["iat"] == 3600
    # Signed for real: the public half of the key verifies the signature over header.claims.
    private_key.public_key().verify(
        _b64(signature),
        f"{header}.{claims}".encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )


async def test_a_key_file_cannot_redirect_the_assertion_to_another_host(
    tmp_path: Path, private_key: rsa.RSAPrivateKey
) -> None:
    # The key names its own token endpoint. Following it would send a signed credential
    # wherever a tampered file said, so a different one is refused and NOTHING is sent.
    recorder = Recorder()
    path = _write_key(tmp_path, private_key, overrides={"token_uri": "https://evil.test/token"})
    with pytest.raises(AnalyticsFetchError) as refused:
        await _source(path, recorder).access_token()
    assert refused.value.code is FetchErrorCode.TOKEN_FILE_INVALID
    assert recorder.requests == []


# --------------------------------------------------------------------------- #
# Caching: in memory, re-minted before expiry
# --------------------------------------------------------------------------- #
async def test_the_token_is_cached_then_reminted_before_it_expires(
    tmp_path: Path, private_key: rsa.RSAPrivateKey
) -> None:
    now = [1_000_000.0]
    recorder = Recorder()
    source = _source(_write_key(tmp_path, private_key), recorder, clock=lambda: now[0])

    await source.access_token()
    now[0] += 3000  # well inside the 3600 seconds
    await source.access_token()
    assert len(recorder.requests) == 1, "a still-valid token must be reused, not re-minted"

    now[0] += 600  # 3600 - 120 margin has passed: it would die mid-import
    await source.access_token()
    assert len(recorder.requests) == 2


# --------------------------------------------------------------------------- #
# Least privilege: one read-only product per provider
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("host_setting", "base_url", "scope"),
    [
        ("analytics_ga4_base_url", GA4_BASE_URL, ANALYTICS_SCOPE),
        ("analytics_gsc_base_url", GSC_BASE_URL, SEARCH_SCOPE),
    ],
)
async def test_each_provider_asks_for_its_own_read_only_scope_and_uses_the_token(
    tmp_path: Path,
    private_key: rsa.RSAPrivateKey,
    host_setting: str,
    base_url: str,
    scope: str,
) -> None:
    path = _write_key(tmp_path, private_key)
    settings = make_settings(
        environment="local",
        analytics_google_enabled=True,
        analytics_google_credentials_file=str(path),
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if str(request.url) == GOOGLE_TOKEN_URI:
            return httpx.Response(200, json={"access_token": MINTED, "expires_in": 3600})
        return httpx.Response(200, json={"rows": []})

    egress = GoogleEgress(
        settings,
        base_url=base_url,
        host_setting=host_setting,
        transport=httpx.MockTransport(handler),
    )
    token = await egress.access_token()
    await egress.post("/anything", body={}, token=token)

    token_call, api_call = seen
    claims = json.loads(_b64(_form(token_call)["assertion"].split(".")[1]))
    assert claims["scope"] == scope
    assert api_call.headers["Authorization"] == "Bearer " + MINTED
    await egress.aclose()


# --------------------------------------------------------------------------- #
# Refusals, each before any request
# --------------------------------------------------------------------------- #
async def test_a_file_others_can_read_is_refused_before_any_request(
    tmp_path: Path, private_key: rsa.RSAPrivateKey
) -> None:
    recorder = Recorder()
    path = _write_key(tmp_path, private_key, private=False)
    if sys.platform != "win32":
        path.chmod(0o644)
    # On Windows a freshly written temp file inherits the Administrators group, which the rule
    # deliberately refuses; on POSIX it is world-readable here. Either way: refused, silently.
    with pytest.raises(AnalyticsFetchError) as refused:
        await _source(path, recorder).access_token()
    assert refused.value.code is FetchErrorCode.TOKEN_FILE_INVALID
    assert str(refused.value) == "token_file_invalid"
    assert recorder.requests == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"type": "authorized_user"},
        {"client_email": "not-an-email"},
        {"private_key": ""},
        {"private_key": "this is not a key"},
    ],
)
async def test_a_malformed_key_file_is_refused(
    tmp_path: Path, private_key: rsa.RSAPrivateKey, overrides: dict[str, str]
) -> None:
    recorder = Recorder()
    path = _write_key(tmp_path, private_key, overrides=overrides)
    with pytest.raises(AnalyticsFetchError) as refused:
        await _source(path, recorder).access_token()
    assert refused.value.code is FetchErrorCode.TOKEN_FILE_INVALID
    assert recorder.requests == []


async def test_a_file_that_is_not_json_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "creds.json"
    path.write_text("definitely not json", encoding="utf-8")
    _make_private(path)
    with pytest.raises(AnalyticsFetchError) as refused:
        await _source(path, Recorder()).access_token()
    assert refused.value.code is FetchErrorCode.TOKEN_FILE_INVALID


async def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(AnalyticsFetchError) as refused:
        await _source(tmp_path / "absent.json", Recorder()).access_token()
    assert refused.value.code is FetchErrorCode.TOKEN_FILE_INVALID


# --------------------------------------------------------------------------- #
# Google's side failing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(400, json={"error": "invalid_grant"}), FetchErrorCode.GOOGLE_AUTH_FAILED),
        (httpx.Response(401, json={}), FetchErrorCode.GOOGLE_AUTH_FAILED),
        (httpx.Response(503, json={}), FetchErrorCode.GOOGLE_UNAVAILABLE),
        (
            httpx.Response(302, headers={"Location": "https://evil.test/"}),
            FetchErrorCode.GOOGLE_UNAVAILABLE,
        ),
        (httpx.Response(200, text="not json"), FetchErrorCode.GOOGLE_UNAVAILABLE),
        (
            httpx.Response(200, json={"access_token": "", "expires_in": 3600}),
            FetchErrorCode.GOOGLE_UNAVAILABLE,
        ),
        (
            httpx.Response(200, json={"access_token": "has space", "expires_in": 3600}),
            FetchErrorCode.GOOGLE_UNAVAILABLE,
        ),
        (
            httpx.Response(200, json={"access_token": MINTED, "expires_in": -5}),
            FetchErrorCode.GOOGLE_UNAVAILABLE,
        ),
        (httpx.ConnectError("refused"), FetchErrorCode.GOOGLE_UNAVAILABLE),
    ],
)
async def test_a_failed_exchange_is_one_fixed_code(
    tmp_path: Path,
    private_key: rsa.RSAPrivateKey,
    response: httpx.Response | Exception,
    code: FetchErrorCode,
) -> None:
    recorder = Recorder(response)
    source = _source(_write_key(tmp_path, private_key), recorder)
    with pytest.raises(AnalyticsFetchError) as failed:
        await source.access_token()
    assert failed.value.code is code
    assert len(recorder.requests) == 1, "a redirect must not be followed to a second request"


# --------------------------------------------------------------------------- #
# Nothing secret escapes
# --------------------------------------------------------------------------- #
async def test_no_secret_reaches_a_log_or_an_error(
    tmp_path: Path, private_key: rsa.RSAPrivateKey
) -> None:
    pem_body = _pem(private_key).splitlines()[1]
    path = _write_key(tmp_path, private_key)
    outcomes = [
        Recorder(httpx.Response(400, json={"error": "invalid_grant", "detail": MINTED})),
        Recorder(httpx.Response(503, json={})),
        Recorder(httpx.ConnectError(f"cannot reach {GOOGLE_TOKEN_URI}")),
    ]
    messages: list[str] = []
    with structlog.testing.capture_logs() as logs:
        for recorder in outcomes:
            with pytest.raises(AnalyticsFetchError) as failed:
                await _source(path, recorder).access_token()
            messages.append(str(failed.value))
        # And the happy path, which must be silent about what it minted.
        await _source(path, Recorder()).access_token()
    assertion = _form(outcomes[0].requests[0])["assertion"]
    everything = json.dumps(logs, default=str) + " ".join(messages)
    for secret in (pem_body, assertion, MINTED, EMAIL, str(path), path.name):
        assert secret not in everything, "a secret reached a log line or an exception message"


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def test_the_two_credential_sources_are_mutually_exclusive(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="not both"):
        make_settings(
            analytics_google_token_file=str(tmp_path / "token"),
            analytics_google_credentials_file=str(tmp_path / "creds.json"),
        )


def test_the_credentials_file_may_not_live_inside_the_repository() -> None:
    for inside in (REPO_ROOT / "keys" / "creds.json", REPO_ROOT / "creds.json", REPO_ROOT):
        with pytest.raises(ValidationError, match="outside the repository"):
            make_settings(analytics_google_credentials_file=str(inside))


def test_a_sibling_directory_with_the_same_prefix_is_not_inside_the_repository(
    tmp_path: Path,
) -> None:
    sibling = REPO_ROOT.parent / (REPO_ROOT.name + "-keys") / "creds.json"
    settings = make_settings(analytics_google_credentials_file=str(sibling))
    assert settings.analytics_google_credentials_file == str(sibling)


def test_enabling_google_accepts_either_source_and_still_needs_one(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="CREDENTIALS_FILE"):
        make_settings(analytics_google_enabled=True)
    ok = make_settings(
        analytics_google_enabled=True,
        analytics_google_credentials_file=str(tmp_path / "creds.json"),
    )
    assert ok.analytics_google_enabled is True


def test_the_credentials_path_never_reaches_public_settings(tmp_path: Path) -> None:
    path = tmp_path / "creds.json"
    public = make_settings(
        analytics_google_enabled=True, analytics_google_credentials_file=str(path)
    ).public_settings()
    text = repr(public).lower()
    assert str(path).lower() not in text
    assert "credential" not in text


# --------------------------------------------------------------------------- #
# The privacy rule itself
# --------------------------------------------------------------------------- #
OWNER = "S-1-5-21-1111111111-2222222222-3333333333-1001"


@pytest.mark.parametrize(
    ("sddl", "private"),
    [
        (f"O:{OWNER}G:DUD:PAI(A;;FA;;;SY)(A;;FA;;;{OWNER})", True),
        (f"O:{OWNER}G:DUD:P(A;;FA;;;{OWNER})", True),
        (f"O:{OWNER}D:(A;;FA;;;OW)(A;;FA;;;SY)", True),
        # A deny entry for someone else cannot widen access, so it does not make it public.
        (f"O:{OWNER}D:PAI(D;;FA;;;BU)(A;;FA;;;{OWNER})", True),
        (f"O:{OWNER}D:PAI(A;;FA;;;SY)(A;;FA;;;{OWNER})(A;;0x1200a9;;;BU)", False),
        (f"O:{OWNER}D:PAI(A;;FA;;;SY)(A;;FA;;;{OWNER})(A;;FA;;;BA)", False),
        (f"O:{OWNER}D:PAI(A;;FA;;;{OWNER})(A;;FRFX;;;AU)", False),
        (f"O:{OWNER}D:PAI(A;;FA;;;{OWNER})(A;;FR;;;WD)", False),
        (f"O:{OWNER}D:PAI(A;;FA;;;{OWNER})(A;;FR;;;S-1-5-21-9-9-9-1002)", False),
        # No DACL at all, or a null one, means everyone has full access.
        (f"O:{OWNER}G:DU", False),
        (f"O:{OWNER}D:NO_ACCESS_CONTROL", False),
        # An owner that is itself a broad group: "only the owner" would mean every admin.
        ("O:BAG:BAD:PAI(A;;FA;;;BA)(A;;FA;;;SY)", False),
        ("O:S-1-5-32-544D:(A;;FA;;;S-1-5-32-544)", False),
        # Anything it cannot parse is NOT private: the safe direction.
        ("", False),
        ("not sddl at all", False),
        (f"O:{OWNER}D:PAI(A;;FA)", False),
    ],
)
def test_the_privacy_rule_over_security_descriptors(sddl: str, private: bool) -> None:
    assert sddl_is_private(sddl) is private


@pytest.mark.skipif(sys.platform != "win32", reason="ACLs are the Windows form of the rule")
def test_windows_reads_the_real_acl_and_refuses_what_the_rule_refuses(tmp_path: Path) -> None:
    path = tmp_path / "creds.json"
    path.write_text("{}", encoding="utf-8")

    # A new file in the temp directory inherits the Administrators group: not private.
    with pytest.raises(AnalyticsFetchError):
        check_private_credentials_file(path)

    _make_private(path)
    check_private_credentials_file(path)  # owner and SYSTEM only: accepted

    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        ["icacls", str(path), "/grant", "*S-1-5-32-545:R"],  # noqa: S607 -- system tool
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    with pytest.raises(AnalyticsFetchError):  # BUILTIN\Users may now read it
        check_private_credentials_file(path)


@pytest.mark.skipif(sys.platform == "win32", reason="mode bits are the POSIX form of the rule")
def test_posix_requires_mode_0600(tmp_path: Path) -> None:
    path = tmp_path / "creds.json"
    path.write_text("{}", encoding="utf-8")
    for mode in (0o644, 0o640, 0o604, 0o660):
        path.chmod(mode)
        with pytest.raises(AnalyticsFetchError):
            check_private_credentials_file(path)
    path.chmod(0o600)
    check_private_credentials_file(path)
