"""URL normalisation and site scope. FROZEN (orchestrator-owned).

Every URL the inventory stores or compares passes through :func:`normalize_url`
first, so two spellings of one address can never become two pages. The rules
are deliberately conservative: a transformation is applied only when it can
never change which resource a server returns.

Applied:

* scheme and host lower-cased; host IDNA-encoded; trailing dot removed;
* default port removed (``:80`` for http, ``:443`` for https);
* empty path becomes ``/``; ``.`` and ``..`` segments resolved (RFC 3986 5.2.4);
* percent-encoding normalised: hex upper-cased, unreserved characters decoded,
  characters that must be encoded (spaces, non-ASCII) encoded;
* fragment removed (never sent to a server);
* well-known tracking parameters removed from the query (``utm_*``, ``gclid`` ...).

Deliberately NOT applied, because each can change the resource served:

* trailing-slash addition or removal;
* query-parameter reordering;
* ``http`` -> ``https`` upgrades, or ``www.`` stripping.

Rejected outright (:class:`InvalidUrlError`): non-http(s) schemes, a missing
host, an invalid port, credentials in the URL (the inventory never
authenticates), and anything longer than ``MAX_URL_LENGTH``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final
from urllib.parse import urljoin, urlsplit, urlunsplit

from ken_contracts.inventory import MAX_URL_LENGTH, SiteScope

_DEFAULT_PORTS: Final = {"http": 80, "https": 443}
_UNRESERVED: Final = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
# Characters left as-is in a path or query: RFC 3986 unreserved + sub-delims + ":" "@" "/" "?".
_PATH_SAFE: Final = _UNRESERVED | frozenset("!$&'()*+,;=:@/")
_QUERY_SAFE: Final = _PATH_SAFE | frozenset("?")
_PERCENT_RE: Final = re.compile(r"%([0-9A-Fa-f]{2})")
# WHATWG URL parsing strips ASCII tab and newline anywhere in the input.
_TAB_NEWLINE: Final = str.maketrans("", "", "\t\n\r")

TRACKING_PARAMS: Final = frozenset(
    {"gclid", "dclid", "fbclid", "msclkid", "yclid", "mc_cid", "mc_eid", "_ga", "_gl"}
)
TRACKING_PREFIXES: Final = ("utm_",)


class InvalidUrlError(ValueError):
    """The value is not an http(s) URL the inventory may store or fetch.

    The message names the rule that failed, never the URL itself (it may be
    long, or carry credentials).
    """


def _encode(text: str, safe: frozenset[str]) -> str:
    """Normalise percent-encoding of one path or query component."""
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "%":
            match = _PERCENT_RE.match(text, index)
            if match:
                decoded = chr(int(match.group(1), 16))
                # Decode an escaped unreserved character; keep everything else
                # escaped, with upper-case hex.
                out.append(decoded if decoded in _UNRESERVED else f"%{match.group(1).upper()}")
                index += 3
                continue
            out.append("%25")  # a bare "%" that starts no escape
        elif char in safe:
            out.append(char)
        else:
            out.extend(f"%{byte:02X}" for byte in char.encode("utf-8"))
        index += 1
    return "".join(out)


def _remove_dot_segments(path: str) -> str:
    """RFC 3986 section 5.2.4."""
    output: list[str] = []
    segments = path.split("/")
    for position, segment in enumerate(segments):
        last = position == len(segments) - 1
        if segment == ".":
            if last:
                output.append("")
            continue
        if segment == "..":
            if len(output) > 1:
                output.pop()
            if last:
                output.append("")
            continue
        output.append(segment)
    result = "/".join(output)
    if path.startswith("/") and not result.startswith("/"):
        result = "/" + result
    return result or "/"


def _is_tracking(name: str) -> bool:
    key = name.lower()
    return key in TRACKING_PARAMS or key.startswith(TRACKING_PREFIXES)


def _normalise_query(query: str) -> str:
    kept: list[str] = []
    for pair in query.split("&"):
        if not pair:
            continue
        name = pair.split("=", 1)[0]
        if _is_tracking(_PERCENT_RE.sub(lambda m: chr(int(m.group(1), 16)), name)):
            continue
        kept.append(_encode(pair, _QUERY_SAFE))
    return "&".join(kept)


def _normalise_host(hostname: str) -> str:
    host = hostname.rstrip(".").lower()
    if not host:
        raise InvalidUrlError("URL has no host")
    if ":" in host:  # IPv6 literal (urlsplit has already removed the brackets)
        return f"[{host}]"
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise InvalidUrlError("URL host is not a valid hostname") from exc


def normalize_url(url: str, *, base: str | None = None) -> str:
    """Return the normalised absolute form of ``url``, resolved against ``base``.

    Raises:
        InvalidUrlError: see the module docstring.
    """
    raw = url.strip().translate(_TAB_NEWLINE)
    try:
        # urljoin parses both arguments and raises ValueError on e.g. "http://[bad",
        # so it belongs inside the try: try_normalize_url must never raise.
        if base is not None:
            raw = urljoin(base.strip(), raw)
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise InvalidUrlError("URL is malformed or has an invalid port") from exc

    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise InvalidUrlError("only http and https URLs are supported")
    if parts.username is not None or parts.password is not None:
        raise InvalidUrlError("URLs carrying credentials are refused")
    if parts.hostname is None:
        raise InvalidUrlError("URL has no host")

    netloc = _normalise_host(parts.hostname)
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        netloc = f"{netloc}:{port}"
    path = _remove_dot_segments(_encode(parts.path or "/", _PATH_SAFE))
    query = _normalise_query(parts.query)

    result = urlunsplit((scheme, netloc, path, query, ""))
    if len(result) > MAX_URL_LENGTH:
        raise InvalidUrlError(f"URL is longer than {MAX_URL_LENGTH} characters")
    return result


def try_normalize_url(url: str, *, base: str | None = None) -> str | None:
    """:func:`normalize_url`, or ``None`` instead of raising."""
    try:
        return normalize_url(url, base=base)
    except InvalidUrlError:
        return None


def url_netloc(normalized_url: str) -> str:
    """``host[:port]`` of an already-normalised URL."""
    return urlsplit(normalized_url).netloc


def normalize_netloc(value: str) -> str:
    """Normalise a bare ``host`` / ``host:port`` exactly as URLs are (default ports dropped).

    ``example.com:443`` and ``example.com`` therefore name the same scope entry.
    """
    text = value.strip()
    if "://" not in text:
        text = f"https://{text}"
    return url_netloc(normalize_url(text))


def build_scope(base_url: str, extra_hosts: Iterable[str] = ()) -> SiteScope:
    """Site scope: the base URL's host plus any explicitly listed hosts."""
    hosts = {url_netloc(normalize_url(base_url))}
    hosts.update(normalize_netloc(host) for host in extra_hosts if host.strip())
    return SiteScope(allowed_hosts=frozenset(hosts))


def in_scope(normalized_url: str, scope: SiteScope) -> bool:
    """Whether an already-normalised URL is on the site."""
    return url_netloc(normalized_url) in scope.allowed_hosts


def path_key(normalized_url: str) -> str:
    """``scheme://netloc/path`` without the query: the crawler-trap grouping key."""
    parts = urlsplit(normalized_url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
