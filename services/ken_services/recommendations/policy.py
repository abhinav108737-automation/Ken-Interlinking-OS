"""Recommendation and ranking policy loading (ADR-0011).

Both files are DATA (``policies/recommendation/recommendation.v<N>.yaml`` and
``policies/ranking/ranking_weights.v<N>.yaml``), read with ``yaml.safe_load`` and validated
by the frozen contracts :class:`~ken_contracts.recommendations.RecommendationPolicy` and
:class:`~ken_contracts.recommendations.RankingPolicy`.

Loads are cached by (resolved path, ``st_mtime_ns``), exactly like the taxonomy loader:
repeated calls return the SAME object until the file changes on disk.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import BaseModel, ValidationError

from ken_contracts.recommendations import RankingPolicy, RecommendationPolicy

__all__ = [
    "PolicyError",
    "load_ranking_policy",
    "load_recommendation_policy",
    "resolve_ranking_policy",
]

_CACHE_SIZE: Final = 8


class PolicyError(ValueError):
    """The policy file is not valid YAML, not a mapping, or fails validation.

    The message names the policy kind and the error count, never file contents.
    """


def _read(resolved: str, what: str) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(Path(resolved).read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PolicyError(f"{what} policy file is not valid YAML") from exc
    if not isinstance(raw, dict):
        raise PolicyError(f"{what} policy file must contain a mapping")
    return raw


def _validate[M: BaseModel](model: type[M], raw: dict[str, Any], what: str) -> M:
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise PolicyError(f"{what} policy file is invalid: {exc.error_count()} error(s)") from exc


@lru_cache(maxsize=_CACHE_SIZE)
def _recommendation_cached(resolved: str, mtime_ns: int) -> RecommendationPolicy:
    del mtime_ns  # part of the cache key only
    return _validate(RecommendationPolicy, _read(resolved, "recommendation"), "recommendation")


@lru_cache(maxsize=_CACHE_SIZE)
def _ranking_cached(resolved: str, mtime_ns: int) -> RankingPolicy:
    del mtime_ns  # part of the cache key only
    return _validate(RankingPolicy, _read(resolved, "ranking"), "ranking")


def _key(path: str | Path) -> tuple[str, int]:
    resolved = Path(path).resolve()
    return str(resolved), resolved.stat().st_mtime_ns


def load_recommendation_policy(path: str | Path) -> RecommendationPolicy:
    """Load and validate a recommendation policy file (cached by path and mtime).

    Raises:
        FileNotFoundError: the file does not exist.
        PolicyError: malformed YAML, not a mapping, or invalid.
    """
    return _recommendation_cached(*_key(path))


def load_ranking_policy(path: str | Path) -> RankingPolicy:
    """Load and validate a ranking-weights policy file (cached by path and mtime).

    Raises:
        FileNotFoundError: the file does not exist.
        PolicyError: malformed YAML, not a mapping, or invalid (weights must name exactly
            the TRD section 12 components and sum to 1).
    """
    return _ranking_cached(*_key(path))


def resolve_ranking_policy(policy: RecommendationPolicy, *, root: str | Path) -> RankingPolicy:
    """The ranking policy a recommendation policy references (repo-relative to ``root``).

    Raises:
        PolicyError: the reference is absolute or escapes ``root``.
        FileNotFoundError / PolicyError: as :func:`load_ranking_policy`.
    """
    base = Path(root).resolve()
    reference = Path(policy.ranking_policy)
    if reference.is_absolute():
        raise PolicyError("ranking_policy must be repo-relative")
    target = (base / reference).resolve()
    if not target.is_relative_to(base):
        raise PolicyError("ranking_policy must stay inside the repository")
    return load_ranking_policy(target)


def _clear_caches() -> None:
    """Test hook: forget every cached policy."""
    _recommendation_cached.cache_clear()
    _ranking_cached.cache_clear()
