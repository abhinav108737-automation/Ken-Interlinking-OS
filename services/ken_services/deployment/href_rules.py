"""Link ``href`` rules for deployment (ADR-0014 section 3). Pure; no I/O, no DNS.

An ``href`` we write into a page must be:

* an absolute ``http`` or ``https`` URL with a host (so never ``javascript:``, ``data:``,
  ``vbscript:``, a relative path or a protocol-relative ``//host``);
* printable ASCII with no whitespace or control characters (browsers strip tabs and newlines
  inside URLs, a classic way to smuggle ``java\\tscript:``) and no backslash (browsers read
  ``\\`` as ``/`` in http URLs);
* free of credentials (``user:pass@host``), with a valid port;
* on an allowed host, when the caller passes ``allowed_hosts`` (the source page's host plus
  the configured extra hosts; the deployment service decides which).

The value is written into a double-quoted attribute through :func:`escape_href`, so ``"``,
``&``, ``<``, ``>`` and ``'`` can never break out of it.
"""

from __future__ import annotations

import html
from collections.abc import Collection
from enum import StrEnum
from typing import Final
from urllib.parse import SplitResult, urlsplit

from ken_contracts.inventory import MAX_URL_LENGTH
from ken_services.inventory.urls import try_normalize_url

__all__ = [
    "ALLOWED_SCHEMES",
    "HrefProblem",
    "escape_href",
    "href_problem",
    "hrefs_equivalent",
]

ALLOWED_SCHEMES: Final = frozenset({"http", "https"})


class HrefProblem(StrEnum):
    """Why an ``href`` is refused (fixed vocabulary, never echoes the URL)."""

    EMPTY = "empty"
    TOO_LONG = "too_long"
    UNSAFE_CHARACTERS = "unsafe_characters"
    UNPARSEABLE = "unparseable"
    SCHEME_NOT_ALLOWED = "scheme_not_allowed"
    CREDENTIALS = "credentials"
    NO_HOST = "no_host"
    INVALID_PORT = "invalid_port"
    HOST_NOT_ALLOWED = "host_not_allowed"


def _host_key(host: str) -> str:
    return host.strip().lower().rstrip(".")


def href_problem(href: str, *, allowed_hosts: Collection[str] | None = None) -> HrefProblem | None:
    """The first rule ``href`` breaks, or ``None`` when it may be written into a page."""
    if not href:
        return HrefProblem.EMPTY
    if len(href) > MAX_URL_LENGTH:
        return HrefProblem.TOO_LONG
    if any(not (0x21 <= ord(char) <= 0x7E) for char in href) or "\\" in href:
        return HrefProblem.UNSAFE_CHARACTERS
    try:
        parts: SplitResult = urlsplit(href)
    except ValueError:
        return HrefProblem.UNPARSEABLE
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        return HrefProblem.SCHEME_NOT_ALLOWED
    if "@" in parts.netloc:
        return HrefProblem.CREDENTIALS
    try:
        port = parts.port  # raises ValueError on a non-numeric or out-of-range port
    except ValueError:
        return HrefProblem.INVALID_PORT
    host = parts.hostname
    if not host:
        return HrefProblem.NO_HOST
    if port == 0:
        return HrefProblem.INVALID_PORT
    if allowed_hosts is not None and _host_key(host) not in {_host_key(h) for h in allowed_hosts}:
        return HrefProblem.HOST_NOT_ALLOWED
    return None


def escape_href(href: str) -> str:
    """``href`` escaped for a double-quoted HTML attribute (``&``, ``<``, ``>``, ``"``, ``'``)."""
    return html.escape(href, quote=True)


def hrefs_equivalent(found: str | None, expected: str | None) -> bool:
    """Whether a link's ``href`` (as parsed from a page) points where we expect.

    Equal after trimming, or both normalise (``ken_services.inventory.urls``) to the same URL
    with the same fragment -- a CMS may lower-case the host or drop a default port.
    """
    if found is None or expected is None:
        return False
    a, b = found.strip(), expected.strip()
    if a == b:
        return True
    norm_a, norm_b = try_normalize_url(a), try_normalize_url(b)
    if norm_a is None or norm_a != norm_b:
        return False
    return urlsplit(a).fragment == urlsplit(b).fragment
