"""Taxonomy policy loading and the precompiled :class:`TaxonomyIndex` (ADR-0010 section 3).

:func:`load_taxonomy` reads ``policies/taxonomy/taxonomy.v<N>.yaml`` with
``yaml.safe_load`` and validates it with the frozen
:class:`~ken_contracts.intelligence.TaxonomyPolicy`. Results are cached by
(resolved path, mtime_ns), so editing the file in place is picked up and an
unchanged file is parsed once.

:func:`taxonomy_index` builds, once per policy object, everything analysis
needs at speed: tree lookups (depth, ancestors, root), compiled page-type and
protection regexes, and one :class:`~ken_services.intelligence.text.PhraseMatcher`
per vocabulary (markets, geographies, entities). A node's own label is an
implicit alias.
"""

from __future__ import annotations

import re
import threading
import weakref
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final

import yaml
from pydantic import ValidationError

from ken_contracts.intelligence import (
    EntityEntry,
    GeographyNode,
    MarketNode,
    PageTypeRule,
    ProtectionRule,
    TaxonomyPolicy,
)
from ken_services.intelligence.text import PhraseMatcher

__all__ = [
    "TaxonomyError",
    "TaxonomyIndex",
    "load_taxonomy",
    "taxonomy_index",
]

_LOAD_CACHE_SIZE: Final = 8
_INDEX_CACHE_SIZE: Final = 8


class TaxonomyError(ValueError):
    """The taxonomy file is unreadable YAML, not a mapping, invalid, or cyclic."""


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def load_taxonomy(path: str | Path) -> TaxonomyPolicy:
    """Load and validate a taxonomy policy file.

    Cached by (resolved path, ``st_mtime_ns``): repeated calls return the SAME
    object until the file changes.

    Raises:
        FileNotFoundError: the file does not exist.
        TaxonomyError: the YAML is malformed, not a mapping, or fails validation.
    """
    resolved = Path(path).resolve()
    mtime_ns = resolved.stat().st_mtime_ns
    return _load_cached(str(resolved), mtime_ns)


@lru_cache(maxsize=_LOAD_CACHE_SIZE)
def _load_cached(resolved: str, mtime_ns: int) -> TaxonomyPolicy:
    del mtime_ns  # part of the cache key only
    try:
        raw = yaml.safe_load(Path(resolved).read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise TaxonomyError("taxonomy file is not valid YAML") from exc
    if not isinstance(raw, dict):
        raise TaxonomyError("taxonomy file must contain a mapping")
    try:
        policy = TaxonomyPolicy.model_validate(raw)
    except ValidationError as exc:
        raise TaxonomyError(f"taxonomy file is invalid: {exc.error_count()} error(s)") from exc
    # Fail at load time, not at first analysis, on a cyclic tree.
    taxonomy_index(policy)
    return policy


def _clear_caches() -> None:
    """Test hook: forget every cached policy and index."""
    _load_cached.cache_clear()
    with _INDEX_LOCK:
        _INDEX_CACHE.clear()


# --------------------------------------------------------------------------- #
# Index
# --------------------------------------------------------------------------- #
def _ancestor_chains(parents: Mapping[str, str | None], what: str) -> dict[str, tuple[str, ...]]:
    """id -> ancestors, nearest first. Raises TaxonomyError on a parent cycle."""
    chains: dict[str, tuple[str, ...]] = {}
    for node_id in sorted(parents):
        chain: list[str] = []
        seen = {node_id}
        current = parents[node_id]
        while current is not None:
            if current in seen:
                raise TaxonomyError(f"{what} tree has a cycle through {current!r}")
            seen.add(current)
            chain.append(current)
            current = parents[current]
        chains[node_id] = tuple(chain)
    return chains


@dataclass(frozen=True, slots=True)
class _CompiledRule[R]:
    rule: R
    patterns: tuple[re.Pattern[str], ...]

    def matches_path(self, path: str) -> bool:
        return any(pattern.search(path) for pattern in self.patterns)


class TaxonomyIndex:
    """Precompiled, read-only view of one :class:`TaxonomyPolicy`.

    Build it through :func:`taxonomy_index` (cached), not directly. All lookups
    of an unknown id raise ``KeyError``; use the ``has_*`` helpers to test.
    """

    __slots__ = (
        "_entities",
        "_geo_ancestors",
        "_geographies",
        "_market_ancestors",
        "_markets",
        "_page_type_rules",
        "_page_types",
        "_protection_rules",
        "default_page_type",
        "entity_matcher",
        "geography_matcher",
        "market_matcher",
        "policy",
    )

    def __init__(self, policy: TaxonomyPolicy) -> None:
        self.policy = policy
        self._markets: dict[str, MarketNode] = {m.id: m for m in policy.markets}
        self._geographies: dict[str, GeographyNode] = {g.id: g for g in policy.geographies}
        self._entities: dict[str, EntityEntry] = {e.id: e for e in policy.entities}
        self._page_types: dict[str, PageTypeRule] = {p.id: p for p in policy.page_types}
        self._market_ancestors = _ancestor_chains(
            {m.id: m.parent for m in policy.markets}, "market"
        )
        self._geo_ancestors = _ancestor_chains(
            {g.id: g.parent for g in policy.geographies}, "geography"
        )
        self._page_type_rules: tuple[_CompiledRule[PageTypeRule], ...] = tuple(
            _CompiledRule(rule, tuple(re.compile(p) for p in rule.path_patterns))
            for rule in policy.page_types
        )
        self._protection_rules: tuple[_CompiledRule[ProtectionRule], ...] = tuple(
            _CompiledRule(rule, tuple(re.compile(p) for p in rule.path_patterns))
            for rule in policy.protections
        )
        self.default_page_type: PageTypeRule = self._page_types[policy.default_page_type]
        self.market_matcher = PhraseMatcher({m.id: (m.label, *m.aliases) for m in policy.markets})
        self.geography_matcher = PhraseMatcher(
            {g.id: (g.label, *g.aliases) for g in policy.geographies}
        )
        self.entity_matcher = PhraseMatcher({e.id: (e.label, *e.aliases) for e in policy.entities})

    # -- markets ----------------------------------------------------------- #
    def has_market(self, market_id: str) -> bool:
        return market_id in self._markets

    def market(self, market_id: str) -> MarketNode:
        return self._markets[market_id]

    def market_ancestors(self, market_id: str) -> tuple[str, ...]:
        """Ancestors of ``market_id``, nearest (parent) first; () for an industry."""
        return self._market_ancestors[market_id]

    def market_parent(self, market_id: str) -> str | None:
        return self._markets[market_id].parent

    def market_depth(self, market_id: str) -> int:
        """0 for an industry (top-level market)."""
        return len(self._market_ancestors[market_id])

    def market_root(self, market_id: str) -> str:
        """The industry (top-level market) containing ``market_id`` (itself if top-level)."""
        chain = self._market_ancestors[market_id]
        return chain[-1] if chain else market_id

    # -- geographies ------------------------------------------------------- #
    def has_geography(self, geo_id: str) -> bool:
        return geo_id in self._geographies

    def geography(self, geo_id: str) -> GeographyNode:
        return self._geographies[geo_id]

    def geography_ancestors(self, geo_id: str) -> tuple[str, ...]:
        """Ancestors of ``geo_id``, nearest first (india: south-asia, asia-pacific, global)."""
        return self._geo_ancestors[geo_id]

    # -- entities ---------------------------------------------------------- #
    def entity(self, entity_id: str) -> EntityEntry:
        return self._entities[entity_id]

    # -- page types and protections --------------------------------------- #
    def has_page_type(self, page_type_id: str) -> bool:
        return page_type_id in self._page_types

    def page_type(self, page_type_id: str) -> PageTypeRule:
        return self._page_types[page_type_id]

    def classify_path(self, path: str) -> PageTypeRule:
        """The first page-type rule with a pattern matching ``path``, else the default.

        Patterns are applied with ``re.search`` (they carry their own anchors).
        An empty path is classified as ``/``.
        """
        target = path or "/"
        for compiled in self._page_type_rules:
            if compiled.matches_path(target):
                return compiled.rule
        return self.default_page_type

    def protection_rules_for_path(self, path: str) -> tuple[ProtectionRule, ...]:
        """Protection rules (declaration order) with a path pattern matching ``path``."""
        target = path or "/"
        return tuple(c.rule for c in self._protection_rules if c.matches_path(target))


_INDEX_LOCK: Final = threading.Lock()
_INDEX_CACHE: dict[int, tuple[weakref.ReferenceType[TaxonomyPolicy], TaxonomyIndex]] = {}


def taxonomy_index(policy: TaxonomyPolicy) -> TaxonomyIndex:
    """The :class:`TaxonomyIndex` of ``policy``, built once per policy object.

    Keyed by object identity (a weak reference guards against id reuse), and
    bounded. Policies are immutable, so identity is a sound key.

    Raises:
        TaxonomyError: the market or geography tree contains a parent cycle.
    """
    key = id(policy)
    with _INDEX_LOCK:
        entry = _INDEX_CACHE.get(key)
        if entry is not None and entry[0]() is policy:
            return entry[1]
    index = TaxonomyIndex(policy)
    with _INDEX_LOCK:
        if len(_INDEX_CACHE) >= _INDEX_CACHE_SIZE:
            _INDEX_CACHE.pop(next(iter(_INDEX_CACHE)))
        _INDEX_CACHE[key] = (weakref.ref(policy), index)
    return index
