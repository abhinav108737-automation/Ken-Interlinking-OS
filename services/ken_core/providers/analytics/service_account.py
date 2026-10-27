"""Google service-account authentication for the read-only GA4 / Search Console providers.

An operator supplies the JSON key of a service account that has been granted READ access to the GA4
property (Viewer) and the Search Console property (user) -- the same kind of key used by other
Google-data projects. The key never leaves this module's call path: it is read from a ``0600`` file,
used to sign a short-lived JWT (RS256), exchanged at Google's token endpoint for an access token,
and discarded. Only the access token is kept, in memory, until shortly before it expires.

Guards (all refuse with a fixed ``FetchErrorCode``, never a message that could carry the key):

* the key file must be a regular file with mode ``0600``, valid JSON, ``type == "service_account"``,
  with a plausible ``client_email`` and a PEM private key;
* the token URI is NOT taken from the file: it is always ``https://oauth2.googleapis.com/token``;
* the token request is a guarded POST: https, exact host, no redirects, no proxy, bounded retry;
* the scopes are read-only (``analytics.readonly`` / ``webmasters.readonly``), passed by the caller;
* the private key, the JWT and the access token never appear in a log, an error or a repr.

Used only when ``KEN_ANALYTICS_SERVICE_ACCOUNT_FILE`` is set and Google egress is enabled
(``KEN_ANALYTICS_GOOGLE_ENABLED``, an ASK action). It sends no page content: only the signed
assertion (service-account email, scope, time) to Google's token endpoint.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Final

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ken_core.logging import get_logger

__all__ = [
    "GA4_SCOPE",
    "GSC_SCOPE",
    "TOKEN_URI",
    "ServiceAccountError",
    "ServiceAccountTokenSource",
    "load_key",
]

logger = get_logger(__name__)

TOKEN_URI: Final = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL, not a secret
GA4_SCOPE: Final = "https://www.googleapis.com/auth/analytics.readonly"
GSC_SCOPE: Final = "https://www.googleapis.com/auth/webmasters.readonly"
_ALLOWED_SCOPES: Final = frozenset({GA4_SCOPE, GSC_SCOPE})
_EMAIL_RE: Final = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9-]+\.iam\.gserviceaccount\.com$")
_MODE_MASK: Final = 0o077
_JWT_LIFETIME_S: Final = 3600
_REFRESH_MARGIN_S: Final = 120
_TRIES: Final = 3
_BACKOFF_S: Final = 0.5


class ServiceAccountError(Exception):
    """A refusal. ``code`` is one of ``key_invalid``, ``auth_failed``, ``unavailable``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _Key:
    """The parsed key. ``repr`` hides everything."""

    __slots__ = ("client_email", "private_key")

    def __init__(self, client_email: str, private_key: rsa.RSAPrivateKey) -> None:
        self.client_email = client_email
        self.private_key = private_key

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "<ServiceAccountKey ***>"


def load_key(path_value: str) -> _Key:
    """Read and validate the key file. Raises ``ServiceAccountError("key_invalid")``."""
    path = Path(path_value)
    try:
        info = path.stat()
        if not path.is_file() or (info.st_mode & _MODE_MASK):
            raise ServiceAccountError("key_invalid")
        data = json.loads(path.read_text(encoding="utf-8"))
    except ServiceAccountError:
        raise
    except (OSError, ValueError):
        raise ServiceAccountError("key_invalid") from None
    if not isinstance(data, dict) or data.get("type") != "service_account":
        raise ServiceAccountError("key_invalid")
    email = data.get("client_email")
    pem = data.get("private_key")
    if not isinstance(email, str) or not _EMAIL_RE.match(email):
        raise ServiceAccountError("key_invalid")
    if not isinstance(pem, str) or "BEGIN PRIVATE KEY" not in pem:
        raise ServiceAccountError("key_invalid")
    try:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
    except (ValueError, TypeError):
        raise ServiceAccountError("key_invalid") from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ServiceAccountError("key_invalid")
    return _Key(email, key)


def _b64(raw: bytes) -> bytes:
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def sign_jwt(key: _Key, *, scope: str, now: int) -> str:
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = _b64(
        json.dumps(
            {
                "iss": key.client_email,
                "scope": scope,
                "aud": TOKEN_URI,
                "iat": now,
                "exp": now + _JWT_LIFETIME_S,
            },
            separators=(",", ":"),
        ).encode()
    )
    signing_input = header + b"." + claims
    signature = key.private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return (signing_input + b"." + _b64(signature)).decode()


class ServiceAccountTokenSource:
    """Mints and caches read-only access tokens from a service-account key file."""

    def __init__(
        self,
        key_path: str,
        *,
        timeout_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._key_path = key_path
        self._timeout_s = timeout_s
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._cache: dict[str, tuple[str, float]] = {}
        self._client: httpx.AsyncClient | None = None

    async def token(self, scope: str) -> str:
        if scope not in _ALLOWED_SCOPES:
            raise ServiceAccountError("key_invalid")
        cached = self._cache.get(scope)
        now = self._clock()
        if cached is not None and cached[1] - _REFRESH_MARGIN_S > now:
            return cached[0]
        key = load_key(self._key_path)  # re-read each time: a rotated key is picked up
        assertion = sign_jwt(key, scope=scope, now=int(now))
        token, lifetime = await self._exchange(assertion)
        self._cache[scope] = (token, now + lifetime)
        return token

    async def _exchange(self, assertion: str) -> tuple[str, float]:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout_s,
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            )
        body = {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}
        for attempt in range(1, _TRIES + 1):
            try:
                response = await self._client.post(TOKEN_URI, data=body)
            except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError, OSError) as exc:
                logger.warning(
                    "analytics_token_request_failed",
                    attempt=attempt,
                    error_class=type(exc).__name__,
                )
                if attempt == _TRIES:
                    raise ServiceAccountError("unavailable") from None
                await self._sleep(_BACKOFF_S * attempt)
                continue
            status = response.status_code
            if status in {400, 401, 403}:
                logger.warning("analytics_token_refused", status=status)
                raise ServiceAccountError("auth_failed")
            if status in {429, 500, 502, 503, 504}:
                if attempt == _TRIES:
                    raise ServiceAccountError("unavailable")
                await self._sleep(_BACKOFF_S * attempt)
                continue
            if status != 200:
                raise ServiceAccountError("unavailable")
            return self._parse(response)
        raise ServiceAccountError("unavailable")

    @staticmethod
    def _parse(response: httpx.Response) -> tuple[str, float]:
        try:
            payload: Any = response.json()
        except ValueError:
            raise ServiceAccountError("unavailable") from None
        token = payload.get("access_token") if isinstance(payload, dict) else None
        lifetime = payload.get("expires_in") if isinstance(payload, dict) else None
        if (
            not isinstance(token, str)
            or not token
            or not token.isascii()
            or any(char.isspace() for char in token)
            or not isinstance(lifetime, (int, float))
            or not 60 <= lifetime <= 7200
        ):
            raise ServiceAccountError("unavailable")
        return token, float(lifetime)

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()
