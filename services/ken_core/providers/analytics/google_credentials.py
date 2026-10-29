"""Service-account credentials for the analytics importers (ADR-0022).

The importers used to need a pre-made OAuth access token in a file. A token lives about an hour,
so that was not a workable operating procedure. This module instead holds the service-account
KEY and mints its own short-lived access token IN MEMORY: no token is ever written to disk.

Two things are deliberately NOT relaxed:

* **The key file must be private to its owner.** On POSIX that is mode ``0600``, as it always
  was. Windows has no mode bits, so it is checked against the file's ACL instead: the only
  principals that may be granted access are the owner and ``SYSTEM``. That is the native
  equivalent, not an exemption. A file that Windows reports as readable by ``Users``,
  ``Authenticated Users``, ``Everyone`` or even ``Administrators`` is refused.
* **Egress rules are unchanged.** The token exchange goes to ONE hard-coded host over https
  through our own client (``follow_redirects=False``, ``trust_env=False``): the ``token_uri``
  inside the key file is never followed, so a tampered key cannot redirect the signed
  assertion to another host. ``google-auth`` is used only to SIGN the assertion; none of its
  HTTP transports are used.

Everything that can fail does so with a fixed :class:`FetchErrorCode`. Neither the key, the
signed assertion, the access token, the account's email nor the file path ever reaches a log
line, an exception message or a counter.
"""

from __future__ import annotations

import asyncio
import json
import re
import stat
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import httpx
from google.auth import crypt, jwt

from ken_core.logging import get_logger
from ken_core.providers.analytics.base import AnalyticsFetchError, FetchErrorCode

__all__ = [
    "GOOGLE_TOKEN_URI",
    "ServiceAccountTokenSource",
    "check_private_credentials_file",
    "sddl_is_private",
]

logger = get_logger(__name__)

GOOGLE_TOKEN_URI: Final = "https://oauth2.googleapis.com/token"  # noqa: S105 -- a URL, not a secret
"""The only place an assertion is ever sent. Never read from the key file."""
_JWT_BEARER: Final = "urn:ietf:params:oauth:grant-type:jwt-bearer"
_ASSERTION_LIFETIME_S: Final = 3600
_REFRESH_MARGIN_S: Final = 120
"""Mint a new token this long before the old one expires, so a fetch never starts with one that
dies mid-import."""
_MAX_KEY_BYTES: Final = 16_384

# --------------------------------------------------------------------------- #
# Windows: is the file private to its owner?
# --------------------------------------------------------------------------- #
_ALLOW_ACE_TYPES: Final = frozenset({"A", "OA", "XA", "ZA"})
"""ACE types that GRANT access. Deny and audit entries cannot widen who may read the file."""
_SYSTEM: Final = frozenset({"SY", "S-1-5-18"})
_OWNER_RIGHTS: Final = frozenset({"OW", "S-1-3-4"})
_BROAD: Final = frozenset(
    {
        # Aliases and SIDs for groups that are never "just the owner": Administrators, Users,
        # Authenticated Users, Everyone, Interactive, Network, Anonymous, Guests.
        "BA",
        "BU",
        "AU",
        "WD",
        "IU",
        "NU",
        "AN",
        "BG",
        "S-1-5-32-544",
        "S-1-5-32-545",
        "S-1-5-11",
        "S-1-1-0",
        "S-1-5-4",
        "S-1-5-2",
        "S-1-5-7",
        "S-1-5-32-546",
    }
)
_MARKER = re.compile(r"([OGDS]):")
_PAREN = re.compile(r"\([^()]*\)")


def _split_sddl(sddl: str) -> dict[str, str]:
    """The top-level ``O:``, ``G:``, ``D:`` and ``S:`` components of an SDDL string.

    Parenthesised ACEs are masked first, so a ``:`` or a ``D`` inside an ACE cannot be mistaken
    for a component marker.
    """
    masked = _PAREN.sub(lambda match: "~" * len(match.group()), sddl)
    marks = [(m.group(1), m.start(), m.end()) for m in _MARKER.finditer(masked)]
    parts: dict[str, str] = {}
    for index, (name, _start, end) in enumerate(marks):
        stop = marks[index + 1][1] if index + 1 < len(marks) else len(sddl)
        parts.setdefault(name, sddl[end:stop])
    return parts


def sddl_is_private(sddl: str) -> bool:
    """Whether a file's security descriptor lets ONLY its owner (and SYSTEM) in.

    Pure, and total: a string it cannot parse is reported as NOT private. That is the safe
    direction, because the caller is deciding whether a key may be read.

    ``sddl`` is the form Windows itself produces (``O:`` owner, ``D:`` discretionary ACL). A
    missing or null DACL means everyone has full access, so it is refused.
    """
    try:
        parts = _split_sddl(sddl)
        owner = parts.get("O", "").strip()
        dacl = parts.get("D")
        if not owner or dacl is None or "NO_ACCESS_CONTROL" in dacl:
            return False
        if owner in _BROAD:
            # Files made by an elevated process are owned by the Administrators GROUP, which
            # would make "the owner" mean every administrator on the machine.
            return False
        allowed = {owner} | _SYSTEM | _OWNER_RIGHTS
        for ace in _PAREN.findall(dacl):
            fields = ace[1:-1].split(";")
            if len(fields) < 6:
                return False
            if fields[0] in _ALLOW_ACE_TYPES and fields[5].strip() not in allowed:
                return False
    except (ValueError, IndexError):
        return False
    return True


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _SE_FILE_OBJECT: Final = 1
    _OWNER_AND_DACL: Final = 0x1 | 0x4
    _SDDL_REVISION_1: Final = 1
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    _advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.c_void_p,
    ]
    _advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p

    def _windows_sddl(path: Path) -> str | None:
        """The owner and DACL of ``path`` as SDDL, straight from the OS, or ``None``."""
        descriptor = ctypes.c_void_p()
        status = _advapi32.GetNamedSecurityInfoW(
            str(path),
            _SE_FILE_OBJECT,
            _OWNER_AND_DACL,
            None,
            None,
            None,
            None,
            ctypes.byref(descriptor),
        )
        if status != 0 or not descriptor.value:
            return None
        text = wintypes.LPWSTR()
        try:
            converted = _advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                descriptor, _SDDL_REVISION_1, _OWNER_AND_DACL, ctypes.byref(text), None
            )
            return text.value if converted else None
        finally:
            if text:
                _kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
            _kernel32.LocalFree(descriptor)


def check_private_credentials_file(path: Path) -> None:
    """Refuse unless ``path`` is a regular file that only its owner can read.

    Raises:
        AnalyticsFetchError: ``TOKEN_FILE_INVALID``, whatever the reason; the reason is not
            reported, because it would describe where a secret lives.
    """
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise AnalyticsFetchError(FetchErrorCode.TOKEN_FILE_INVALID)
        if sys.platform == "win32":
            sddl = _windows_sddl(path)
            if sddl is None or not sddl_is_private(sddl):
                raise AnalyticsFetchError(FetchErrorCode.TOKEN_FILE_INVALID)
        elif info.st_mode & 0o077:
            raise AnalyticsFetchError(FetchErrorCode.TOKEN_FILE_INVALID)
    except AnalyticsFetchError:
        raise
    except OSError as exc:
        logger.error("analytics_credentials_file_unreadable", error_class=type(exc).__name__)
        raise AnalyticsFetchError(FetchErrorCode.TOKEN_FILE_INVALID) from None


# --------------------------------------------------------------------------- #
# The token source
# --------------------------------------------------------------------------- #
class ServiceAccountTokenSource:
    """Mints and caches one read-only access token, in memory only.

    One instance per scope, so the Search Console provider cannot use a token that can also read
    Analytics, and the reverse. The key file is re-checked and re-read every time a NEW token is
    minted, never held after the call, and the token is dropped when the object is.
    """

    def __init__(
        self,
        *,
        path: Path,
        scope: str,
        timeout_s: float,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = path
        self._scope = scope
        self._timeout_s = timeout_s
        self._transport = transport
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def access_token(self) -> str:
        """A token valid for at least the refresh margin, minting a new one only if needed."""
        async with self._lock:
            now = self._clock()
            if self._token is not None and now < self._expires_at - _REFRESH_MARGIN_S:
                return self._token
            check_private_credentials_file(self._path)
            assertion = self._sign(self._read_key(), now)
            token, lifetime = await self._exchange(assertion)
            self._token = token
            self._expires_at = now + lifetime
            return token

    # ----------------------------------------------------------------- key
    def _read_key(self) -> dict[str, Any]:
        try:
            if self._path.stat().st_size > _MAX_KEY_BYTES:
                raise AnalyticsFetchError(FetchErrorCode.TOKEN_FILE_INVALID)
            info = json.loads(self._path.read_text(encoding="utf-8"))
        except AnalyticsFetchError:
            raise
        except (OSError, ValueError):
            raise AnalyticsFetchError(FetchErrorCode.TOKEN_FILE_INVALID) from None
        if not isinstance(info, dict) or info.get("type") != "service_account":
            raise AnalyticsFetchError(FetchErrorCode.TOKEN_FILE_INVALID)
        email = info.get("client_email")
        key = info.get("private_key")
        if not (isinstance(email, str) and email.isascii() and "@" in email):
            raise AnalyticsFetchError(FetchErrorCode.TOKEN_FILE_INVALID)
        if not (isinstance(key, str) and key.strip()):
            raise AnalyticsFetchError(FetchErrorCode.TOKEN_FILE_INVALID)
        # The key file names its own token endpoint. It is NEVER followed: a tampered key must
        # not be able to send the signed assertion to a host of the attacker's choosing.
        declared = info.get("token_uri")
        if declared is not None and declared != GOOGLE_TOKEN_URI:
            raise AnalyticsFetchError(FetchErrorCode.TOKEN_FILE_INVALID)
        return info

    def _sign(self, info: dict[str, Any], now: float) -> str:
        issued = int(now)
        claims = {
            "iss": info["client_email"],
            "scope": self._scope,
            "aud": GOOGLE_TOKEN_URI,
            "iat": issued,
            "exp": issued + _ASSERTION_LIFETIME_S,
        }
        try:
            # google-auth is untyped at these two call sites; the result is checked by the
            # `str(...)` and by the exchange, which Google rejects if the signature is wrong.
            signer = crypt.RSASigner.from_service_account_info(info)  # type: ignore[no-untyped-call]
            return str(jwt.encode(signer, claims).decode("ascii"))  # type: ignore[no-untyped-call]
        except (ValueError, KeyError, TypeError):
            raise AnalyticsFetchError(FetchErrorCode.TOKEN_FILE_INVALID) from None

    # ------------------------------------------------------------ exchange
    async def _exchange(self, assertion: str) -> tuple[str, float]:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s,
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    GOOGLE_TOKEN_URI, data={"grant_type": _JWT_BEARER, "assertion": assertion}
                )
        except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError, OSError) as exc:
            # The class name only: an httpx message can carry the URL.
            logger.warning("analytics_token_exchange_failed", error_class=type(exc).__name__)
            raise AnalyticsFetchError(FetchErrorCode.GOOGLE_UNAVAILABLE) from None
        status = response.status_code
        if status in {400, 401, 403}:
            # A revoked or disabled key, a deleted account, or a clock far from true time.
            logger.warning("analytics_token_exchange_refused", status=status)
            raise AnalyticsFetchError(FetchErrorCode.GOOGLE_AUTH_FAILED)
        if status != 200:
            logger.warning("analytics_token_exchange_unexpected_status", status=status)
            raise AnalyticsFetchError(FetchErrorCode.GOOGLE_UNAVAILABLE)
        try:
            payload = response.json()
        except ValueError:
            raise AnalyticsFetchError(FetchErrorCode.GOOGLE_UNAVAILABLE) from None
        token = payload.get("access_token") if isinstance(payload, dict) else None
        lifetime = payload.get("expires_in") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            raise AnalyticsFetchError(FetchErrorCode.GOOGLE_UNAVAILABLE)
        if isinstance(lifetime, bool) or not isinstance(lifetime, int | float) or lifetime <= 0:
            raise AnalyticsFetchError(FetchErrorCode.GOOGLE_UNAVAILABLE)
        return token, float(lifetime)
