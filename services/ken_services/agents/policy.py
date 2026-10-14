"""Agent supervisor policy loading (ADR-0012 section 10).

``policies/agents/agents.v<N>.yaml`` is DATA, read with ``yaml.safe_load`` and validated by
the frozen :class:`~ken_contracts.agents.AgentPolicy`. At load it is CROSS-CHECKED against
the recommendation policy: a run may review at most as many plans as the engine may keep per
source (``max_plans_per_run <= selection.max_plans_per_source``).

Loads are cached by (resolved path, ``st_mtime_ns``) like the other policy loaders; the
cross-check runs on every call (it is cheap and its input may be a different policy).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import ValidationError

from ken_contracts.agents import AgentPolicy
from ken_contracts.recommendations import RecommendationPolicy
from ken_core.config import Settings
from ken_services.recommendations.policy import PolicyError, load_recommendation_policy

__all__ = [
    "AgentPolicyMismatchError",
    "PolicyError",
    "load_agent_policy",
    "load_configured_agent_policy",
]

_CACHE_SIZE: Final = 8


class AgentPolicyMismatchError(PolicyError):
    """The agent policy is valid on its own but inconsistent with the recommendation policy.

    The message names the two numbers only, never file contents or paths.
    """

    def __init__(self, *, max_plans_per_run: int, max_plans_per_source: int) -> None:
        self.max_plans_per_run = max_plans_per_run
        self.max_plans_per_source = max_plans_per_source
        super().__init__(
            f"agent policy max_plans_per_run ({max_plans_per_run}) exceeds the recommendation "
            f"policy's selection.max_plans_per_source ({max_plans_per_source})"
        )


@lru_cache(maxsize=_CACHE_SIZE)
def _agent_cached(resolved: str, mtime_ns: int) -> AgentPolicy:
    del mtime_ns  # part of the cache key only
    try:
        raw: Any = yaml.safe_load(Path(resolved).read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PolicyError("agent policy file is not valid YAML") from exc
    if not isinstance(raw, dict):
        raise PolicyError("agent policy file must contain a mapping")
    try:
        return AgentPolicy.model_validate(raw)
    except ValidationError as exc:
        raise PolicyError(f"agent policy file is invalid: {exc.error_count()} error(s)") from exc


def _key(path: str | Path) -> tuple[str, int]:
    resolved = Path(path).resolve()
    return str(resolved), resolved.stat().st_mtime_ns


def cross_check(policy: AgentPolicy, recommendation: RecommendationPolicy) -> AgentPolicy:
    """Raise :class:`AgentPolicyMismatchError` unless the two policies are consistent."""
    cap = recommendation.selection.max_plans_per_source
    if policy.max_plans_per_run > cap:
        raise AgentPolicyMismatchError(
            max_plans_per_run=policy.max_plans_per_run, max_plans_per_source=cap
        )
    return policy


def load_agent_policy(
    path: str | Path,
    *,
    recommendation_policy: RecommendationPolicy | None = None,
    recommendation_policy_path: str | Path | None = None,
) -> AgentPolicy:
    """Load, validate and cross-check an agent policy file.

    The recommendation policy to check against is ``recommendation_policy`` if given, else
    the file at ``recommendation_policy_path``, else the configured default
    (``Settings().recommendation_policy_path`` default, i.e. ``recommendation.v1.yaml``).

    Raises:
        FileNotFoundError: a policy file does not exist.
        PolicyError: malformed YAML, not a mapping, or invalid.
        AgentPolicyMismatchError: ``max_plans_per_run`` exceeds the engine's per-source cap.
    """
    policy = _agent_cached(*_key(path))
    if recommendation_policy is None:
        if recommendation_policy_path is None:
            default = Settings.model_fields["recommendation_policy_path"].default
            recommendation_policy_path = str(default)
        recommendation_policy = load_recommendation_policy(recommendation_policy_path)
    return cross_check(policy, recommendation_policy)


def load_configured_agent_policy(settings: Settings) -> AgentPolicy:
    """The agent policy at ``KEN_AGENTS_POLICY_PATH``, cross-checked against
    ``KEN_RECOMMENDATION_POLICY_PATH``."""
    return load_agent_policy(
        settings.agents_policy_path,
        recommendation_policy_path=settings.recommendation_policy_path,
    )


def _clear_caches() -> None:
    """Test hook: forget every cached policy."""
    _agent_cached.cache_clear()
