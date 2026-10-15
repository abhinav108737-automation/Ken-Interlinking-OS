"""Analytics policy loading (ADR-0015 section 5).

``policies/analytics/analytics.v<N>.yaml`` is DATA, read with ``yaml.safe_load`` and validated
by the frozen :class:`~ken_contracts.analytics.AnalyticsPolicy`. Loads are cached by (resolved
path, ``st_mtime_ns``) like the other policy loaders.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import ValidationError

from ken_contracts.analytics import AnalyticsPolicy
from ken_core.config import Settings
from ken_services.recommendations.policy import PolicyError

__all__ = ["PolicyError", "load_analytics_policy", "load_configured_analytics_policy"]

_CACHE_SIZE: Final = 8


@lru_cache(maxsize=_CACHE_SIZE)
def _analytics_cached(resolved: str, mtime_ns: int) -> AnalyticsPolicy:
    del mtime_ns  # part of the cache key only
    try:
        raw: Any = yaml.safe_load(Path(resolved).read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PolicyError("analytics policy file is not valid YAML") from exc
    if not isinstance(raw, dict):
        raise PolicyError("analytics policy file must contain a mapping")
    try:
        return AnalyticsPolicy.model_validate(raw)
    except ValidationError as exc:
        raise PolicyError(
            f"analytics policy file is invalid: {exc.error_count()} error(s)"
        ) from exc


def load_analytics_policy(path: str | Path) -> AnalyticsPolicy:
    """Load and validate an analytics policy file (cached by path and mtime).

    Raises:
        FileNotFoundError: the file does not exist.
        PolicyError: malformed YAML, not a mapping, or invalid.
    """
    resolved = Path(path).resolve()
    return _analytics_cached(str(resolved), resolved.stat().st_mtime_ns)


def load_configured_analytics_policy(settings: Settings) -> AnalyticsPolicy:
    """The analytics policy at ``KEN_ANALYTICS_POLICY_PATH``."""
    return load_analytics_policy(settings.analytics_policy_path)


def _clear_caches() -> None:
    """Test hook: forget every cached policy."""
    _analytics_cached.cache_clear()
