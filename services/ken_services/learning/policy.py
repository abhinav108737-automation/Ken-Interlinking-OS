"""Learning gate-policy loading (ADR-0016 sections 7 and 15.7.5).

``policies/learning/learning.v<N>.yaml`` is DATA, read with ``yaml.safe_load`` and validated by
the frozen :class:`~ken_contracts.learning.LearningPolicy`. Loads are cached by (resolved path,
``st_mtime_ns``) like every other policy loader.

The validation is the point: the document may NARROW the change allowlist and ADD frozen paths,
and it can neither widen the allowlist, drop a frozen path, waive a hard-fail suite nor lower
``min_sample_count`` below 30. A document that tries raises :class:`PolicyError` at load, so a
misconfigured file stops the process instead of quietly enlarging what learning may change.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import ValidationError

from ken_contracts.learning import LearningPolicy
from ken_core.config import Settings
from ken_services.recommendations.policy import PolicyError

__all__ = ["PolicyError", "load_configured_learning_policy", "load_learning_policy"]

_CACHE_SIZE: Final = 8


@lru_cache(maxsize=_CACHE_SIZE)
def _learning_cached(resolved: str, mtime_ns: int) -> LearningPolicy:
    del mtime_ns  # part of the cache key only
    try:
        raw: Any = yaml.safe_load(Path(resolved).read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PolicyError("learning policy file is not valid YAML") from exc
    if not isinstance(raw, dict):
        raise PolicyError("learning policy file must contain a mapping")
    try:
        return LearningPolicy.model_validate(raw)
    except ValidationError as exc:
        raise PolicyError(f"learning policy file is invalid: {exc.error_count()} error(s)") from exc


def load_learning_policy(path: str | Path) -> LearningPolicy:
    """Load and validate a learning policy file (cached by path and mtime).

    Raises:
        FileNotFoundError: the file does not exist.
        PolicyError: malformed YAML, not a mapping, or invalid -- including an attempt to
            widen the change allowlist, drop a frozen path or waive a hard-fail suite.
    """
    resolved = Path(path).resolve()
    return _learning_cached(str(resolved), resolved.stat().st_mtime_ns)


def load_configured_learning_policy(settings: Settings) -> LearningPolicy:
    """The learning policy at ``KEN_LEARNING_POLICY_PATH``."""
    return load_learning_policy(settings.learning_policy_path)


def _clear_caches() -> None:
    """Test hook: forget every cached policy."""
    _learning_cached.cache_clear()
