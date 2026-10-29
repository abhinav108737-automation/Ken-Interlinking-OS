"""Autonomy policy loading (ADR-0018; the ``ken_services/learning/policy.py`` pattern).

``policies/autonomy/autonomy.v<N>.yaml`` is DATA, read with ``yaml.safe_load`` and validated by
the frozen :class:`~ken_contracts.autonomy.AutonomyPolicy`. Loads are cached by (resolved path,
``st_mtime_ns``) like every other policy loader.

The validation is the point. The document may make the system MORE conservative than the
contract's hard floors and never less: a confidence floor below
:data:`~ken_contracts.autonomy.GUARDED_CONFIDENCE_FLOOR_MIN`, a retry ceiling above
:data:`~ken_contracts.autonomy.PUBLISH_ATTEMPTS_CEILING`, a cool-down under
:data:`~ken_contracts.autonomy.COOLDOWN_FLOOR_S` or a commercial page type removed raises
:class:`PolicyError` at load, so a misconfigured file stops the process instead of quietly
publishing more.

:func:`effective_retry` is where the policy meets the two ``KEN_AUTONOMY_PUBLISH_*`` settings:
the STRICTER of each pair wins, so an environment variable can tighten the policy and never
loosen it.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import ValidationError

from ken_contracts.autonomy import AutonomyPolicy, RetryPolicy
from ken_core.config import REPO_ROOT, Settings
from ken_services.recommendations.policy import PolicyError

__all__ = [
    "DEFAULT_AUTONOMY_POLICY_PATH",
    "PolicyError",
    "effective_retry",
    "load_autonomy_policy",
    "load_configured_autonomy_policy",
]

_CACHE_SIZE: Final = 8
DEFAULT_AUTONOMY_POLICY_PATH: Final = REPO_ROOT / "policies" / "autonomy" / "autonomy.v1.yaml"
"""The released policy. There is deliberately no ``Settings`` field for the path: pointing the
process at a different file is how a safety document gets swapped out, and every other policy
path is already one more thing an operator can mis-set."""


@lru_cache(maxsize=_CACHE_SIZE)
def _autonomy_cached(resolved: str, mtime_ns: int) -> AutonomyPolicy:
    del mtime_ns  # part of the cache key only
    try:
        raw: Any = yaml.safe_load(Path(resolved).read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PolicyError("autonomy policy file is not valid YAML") from exc
    if not isinstance(raw, dict):
        raise PolicyError("autonomy policy file must contain a mapping")
    try:
        return AutonomyPolicy.model_validate(raw)
    except ValidationError as exc:
        # The count only: a message could quote the offending number back into a log, and the
        # reason is always one of the named floors.
        raise PolicyError(f"autonomy policy file is invalid: {exc.error_count()} error(s)") from exc


def load_autonomy_policy(path: str | Path | None = None) -> AutonomyPolicy:
    """Load and validate an autonomy policy file (cached by path and mtime).

    Raises:
        FileNotFoundError: the file does not exist.
        PolicyError: malformed YAML, not a mapping, or invalid -- including any value that is
            LESS conservative than the contract's hard floor.
    """
    resolved = Path(path if path is not None else DEFAULT_AUTONOMY_POLICY_PATH).resolve()
    return _autonomy_cached(str(resolved), resolved.stat().st_mtime_ns)


def load_configured_autonomy_policy(settings: Settings | None = None) -> AutonomyPolicy:
    """The released autonomy policy. ``settings`` is accepted for call-site symmetry with the
    other loaders and deliberately unused (see :data:`DEFAULT_AUTONOMY_POLICY_PATH`)."""
    del settings
    return load_autonomy_policy()


def effective_retry(policy: AutonomyPolicy, settings: Settings) -> RetryPolicy:
    """The retry budget in force: the STRICTER of the policy and the environment, per value.

    Fewer attempts and a longer cool-down are both stricter, so the result can only be at or
    beyond the policy. The environment is validated by ``Settings`` on its own ranges, which
    allow values the policy floor would refuse (a zero cool-down); taking the stricter of the
    two is what keeps the floor a floor.
    """
    return RetryPolicy(
        max_attempts=min(policy.retry.max_attempts, settings.autonomy_publish_max_attempts),
        cooldown_s=max(policy.retry.cooldown_s, settings.autonomy_publish_cooldown_s),
    )
