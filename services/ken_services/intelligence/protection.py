"""Policy protection rules (ADR-0010 section 5).

A rule protects a page when ANY of its ``path_patterns`` matches the URL path
(``urlsplit(url).path``, ``re.search``) OR -- when it declares
``metadata_equals`` -- EVERY declared key is present in the page metadata with
exactly that value. Matches come back in policy declaration order and always
carry :attr:`ProtectionSource.POLICY`; CMS and manual protections are recorded
elsewhere. Pure and deterministic.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from ken_contracts.intelligence import ProtectionRule, ProtectionSource, TaxonomyPolicy
from ken_services.intelligence.taxonomy import taxonomy_index

__all__ = ["ProtectionMatch", "match_protections"]


@dataclass(frozen=True, slots=True)
class ProtectionMatch:
    rule_id: str
    reason: str
    source: ProtectionSource


def _metadata_matches(rule: ProtectionRule, metadata: Mapping[str, str]) -> bool:
    if not rule.metadata_equals:
        return False
    return all(metadata.get(key) == value for key, value in rule.metadata_equals.items())


def _path_of(url: str) -> str:
    try:
        return urlsplit(url).path or "/"
    except ValueError:
        return "/"


def match_protections(
    *, url: str, metadata: Mapping[str, str], taxonomy: TaxonomyPolicy
) -> tuple[ProtectionMatch, ...]:
    """Every policy protection rule that applies to the page, in declaration order."""
    index = taxonomy_index(taxonomy)
    path_hits = {rule.id for rule in index.protection_rules_for_path(_path_of(url))}
    return tuple(
        ProtectionMatch(rule_id=rule.id, reason=rule.reason, source=ProtectionSource.POLICY)
        for rule in taxonomy.protections
        if rule.id in path_hits or _metadata_matches(rule, metadata)
    )
