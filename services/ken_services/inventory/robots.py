"""RFC 9309 robots.txt parsing and matching. Owned by the crawler workstream.

Why not ``urllib.robotparser``: it implements neither the ``*`` / ``$``
wildcards nor longest-match precedence, so it would silently ignore common
``Disallow`` patterns (ADR-0009, alternatives considered).

Rules implemented (RFC 9309):

* A group starts with one or more consecutive ``user-agent`` lines; the
  ``allow`` / ``disallow`` / ``crawl-delay`` lines that follow belong to it. A
  ``user-agent`` line after a rule starts a new group. Rules before any
  ``user-agent`` line are ignored.
* Field names are case-insensitive; comments, unknown fields, a UTF-8 BOM and
  malformed lines are ignored.
* The crawler's product token (the user agent up to the first ``/`` or
  whitespace) is matched case-insensitively against group names. Every
  matching group is merged; with no match, every ``*`` group is merged; with
  neither, everything is allowed.
* The longest matching pattern wins; on a tie ``allow`` wins. ``*`` matches
  any sequence, a trailing ``$`` anchors the end. An empty ``disallow``
  allows everything. ``/robots.txt`` is always allowed.
* Paths and patterns are compared after the same percent-encoding
  normalisation (upper-case hex, unreserved characters decoded, non-ASCII and
  control characters encoded), so ``%7e`` and ``~`` match each other.
* ``crawl-delay`` (non-standard) is parsed per group as a finite float >= 0;
  merged groups keep the largest value.

Matching uses a linear two-pointer wildcard algorithm, not regular
expressions, so a hostile pattern with many ``*`` cannot cause catastrophic
backtracking.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Final

ROBOTS_PATH: Final = "/robots.txt"
MAX_ROBOTS_BYTES: Final = 500 * 1024
"""Only this much of a robots.txt body is parsed; the rest is ignored (RFC 9309 2.5)."""

MAX_PATTERN_LENGTH: Final = 2048
"""Longer patterns are ignored: no URL the crawler fetches is longer (MAX_URL_LENGTH)."""
MAX_RULES: Final = 2000
"""Rules kept per robots.txt; the rest are ignored. Bounds per-URL matching cost."""

_UNRESERVED: Final = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_PERCENT_RE: Final = re.compile(r"%([0-9A-Fa-f]{2})")


def normalize_robots_path(text: str) -> str:
    """Percent-encoding normalisation applied to both patterns and request paths.

    ``*`` and ``$`` are left untouched (they are pattern syntax in rules and
    ordinary characters in paths; the matcher treats them accordingly).
    """
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "%":
            match = _PERCENT_RE.match(text, index)
            if match:
                decoded = chr(int(match.group(1), 16))
                out.append(decoded if decoded in _UNRESERVED else f"%{match.group(1).upper()}")
                index += 3
                continue
            out.append("%25")
        elif " " < char < "\x7f":
            out.append(char)
        else:
            out.extend(f"%{byte:02X}" for byte in char.encode("utf-8"))
        index += 1
    return "".join(out)


def _wildcard_match(pattern: str, text: str, *, anchored_end: bool) -> bool:
    """Match ``pattern`` (``*`` = any sequence) from the start of ``text``.

    ``anchored_end`` requires the match to reach the end of ``text`` (a ``$``
    rule); otherwise matching a prefix is enough. Splitting on ``*`` and
    finding each literal segment leftmost-first is exact for ``*``-only globs
    and runs in linear time with C-speed ``str.find`` -- the earlier
    backtracking matcher was O(pattern x path) per rule, which a hostile
    robots.txt could turn into seconds of event-loop CPU per URL (QA M3).
    """
    segments = pattern.split("*")
    first, last = segments[0], segments[-1]
    if not text.startswith(first):
        return False
    if len(segments) == 1:
        return len(text) == len(first) if anchored_end else True
    position = len(first)
    for segment in segments[1:-1]:
        if segment:
            found = text.find(segment, position)
            if found < 0:
                return False
            position = found + len(segment)
    if anchored_end:
        return len(text) - len(last) >= position and text.endswith(last)
    return not last or text.find(last, position) >= 0


@dataclass(frozen=True, slots=True)
class RobotsRule:
    """One ``allow`` / ``disallow`` line, pattern already normalised."""

    allow: bool
    pattern: str

    @property
    def length(self) -> int:
        """Precedence key: the pattern's length as written (wildcards included)."""
        return len(self.pattern)

    def matches(self, path: str) -> bool:
        """Whether this rule applies to an already-normalised path (and query)."""
        if self.pattern.endswith("$"):
            return _wildcard_match(self.pattern[:-1], path, anchored_end=True)
        # Unanchored: the pattern only has to match a prefix of the path.
        return _wildcard_match(self.pattern, path, anchored_end=False)


@dataclass(frozen=True, slots=True)
class RobotsPolicy:
    """The merged rules that apply to one crawler on one origin."""

    rules: tuple[RobotsRule, ...] = ()
    crawl_delay: float | None = None

    def can_fetch(self, path_and_query: str) -> bool:
        """Whether ``/path?query`` may be fetched. Longest match wins; allow wins ties."""
        path = path_and_query or "/"
        if not path.startswith("/"):
            path = "/" + path
        if path == ROBOTS_PATH:
            return True
        normalised = normalize_robots_path(path)
        best: RobotsRule | None = None
        for rule in self.rules:
            if not rule.matches(normalised):
                continue
            if (
                best is None
                or rule.length > best.length
                or (rule.length == best.length and rule.allow and not best.allow)
            ):
                best = rule
        return best is None or best.allow


def allow_all() -> RobotsPolicy:
    """No restrictions (robots.txt answered 4xx, or no group applies)."""
    return RobotsPolicy()


def disallow_all() -> RobotsPolicy:
    """Everything except ``/robots.txt`` is disallowed (robots.txt unreachable or 5xx)."""
    return RobotsPolicy(rules=(RobotsRule(allow=False, pattern="/"),))


@dataclass(frozen=True, slots=True)
class RobotsGroup:
    """One group: the user agents it names (lower-cased) and its rules."""

    agents: tuple[str, ...]
    rules: tuple[RobotsRule, ...]
    crawl_delay: float | None = None


def product_token(user_agent: str) -> str:
    """``KenInterlinkBot/0.1 (+https://...)`` -> ``keninterlinkbot`` (lower-cased)."""
    first = user_agent.strip().split(None, 1)[0] if user_agent.strip() else ""
    return first.split("/", 1)[0].lower()


@dataclass(frozen=True, slots=True)
class RobotsRules:
    """A parsed robots.txt."""

    groups: tuple[RobotsGroup, ...] = ()

    def policy_for(self, user_agent: str) -> RobotsPolicy:
        """Merge the groups that apply to ``user_agent`` (see the module docstring)."""
        token = product_token(user_agent)
        matching = [g for g in self.groups if token and token in g.agents]
        if not matching:
            matching = [g for g in self.groups if "*" in g.agents]
        if not matching:
            return allow_all()
        rules = tuple(rule for group in matching for rule in group.rules)
        delays = [g.crawl_delay for g in matching if g.crawl_delay is not None]
        return RobotsPolicy(rules=rules, crawl_delay=max(delays) if delays else None)


def _agent_name(value: str) -> str:
    """Group name as compared: lower-cased, up to the first ``/`` or whitespace."""
    text = value.strip()
    if not text:
        return ""
    return text.split(None, 1)[0].split("/", 1)[0].lower()


def _pattern(value: str) -> str | None:
    """Normalised pattern, or None for an empty value (which matches nothing)."""
    text = value.strip()
    if not text:
        return None
    if not text.startswith(("/", "*")):
        text = "/" + text  # lenient (and more polite) reading of "Disallow: private"
    return normalize_robots_path(text)


def _crawl_delay(value: str) -> float | None:
    try:
        delay = float(value.strip())
    except ValueError:
        return None
    if not math.isfinite(delay) or delay < 0:
        return None
    return delay


@dataclass
class _Builder:
    agents: list[str]
    rules: list[RobotsRule]
    crawl_delay: float | None = None

    def build(self) -> RobotsGroup:
        return RobotsGroup(
            agents=tuple(self.agents), rules=tuple(self.rules), crawl_delay=self.crawl_delay
        )


def parse_robots(text: str) -> RobotsRules:
    """Parse a robots.txt body. Never raises; garbage lines are ignored."""
    if text.startswith("﻿"):
        text = text[1:]
    groups: list[RobotsGroup] = []
    current: _Builder | None = None
    in_agent_lines = False
    kept_rules = 0

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, value = line.split(":", 1)
        field = field.strip().lower()
        value = value.strip()

        if field in {"user-agent", "useragent", "user agent"}:
            if current is None or not in_agent_lines:
                if current is not None:
                    groups.append(current.build())
                current = _Builder(agents=[], rules=[])
            name = _agent_name(value)
            if name:
                current.agents.append(name)
            in_agent_lines = True
            continue

        if current is None:
            continue  # rules before any user-agent line belong to no group
        if field in {"allow", "disallow"}:
            in_agent_lines = False
            pattern = _pattern(value)
            usable = pattern is not None and len(pattern) <= MAX_PATTERN_LENGTH
            if pattern is not None and usable and kept_rules < MAX_RULES:
                current.rules.append(RobotsRule(allow=field == "allow", pattern=pattern))
                kept_rules += 1
        elif field in {"crawl-delay", "crawldelay", "crawl_delay"}:
            in_agent_lines = False
            delay = _crawl_delay(value)
            if delay is not None:
                current.crawl_delay = (
                    delay if current.crawl_delay is None else max(current.crawl_delay, delay)
                )
        # Unknown fields (sitemap, host, ...) neither end nor extend a group's agent list.

    if current is not None:
        groups.append(current.build())
    return RobotsRules(groups=tuple(groups))
