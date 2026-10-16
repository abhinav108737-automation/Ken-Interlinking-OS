"""Phase 8 learning settings (ADR-0016 15.7.10 / 16.1).

``learning_demonstration_mode`` is the mechanism that makes "Phase 8 promotes nothing" true by
construction rather than by promise, so its DEFAULT is the thing worth testing: the corpus is
~65 synthetic pages and the one stream the design calls trustworthy -- human editorial
decisions -- is the stream with no real data.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ken_core.config import REPO_ROOT, Settings


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_demonstration_mode_defaults_to_true() -> None:
    assert _settings().learning_demonstration_mode is True


def test_demonstration_mode_is_an_explicit_operator_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEN_LEARNING_DEMONSTRATION_MODE", "false")
    assert _settings().learning_demonstration_mode is False


def test_settings_are_frozen_so_demonstration_mode_cannot_be_flipped_at_runtime() -> None:
    settings = _settings()
    with pytest.raises(Exception, match=r"frozen"):
        settings.learning_demonstration_mode = False  # type: ignore[misc]


def test_the_learning_policy_path_defaults_into_the_repository() -> None:
    settings = _settings()
    path = Path(settings.learning_policy_path)
    assert path.is_absolute()
    assert path == REPO_ROOT / "policies" / "learning" / "learning.v1.yaml"
    assert path.exists()


def test_a_relative_learning_policy_path_resolves_against_the_repository_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEN_LEARNING_POLICY_PATH", "policies/learning/learning.v1.yaml")
    assert Path(_settings().learning_policy_path).is_absolute()


def test_the_learning_policy_path_is_not_published_in_meta() -> None:
    """``/meta`` is an allowlist, never a dump: a filesystem path is not public settings."""
    public = _settings().public_settings()
    assert "learning_policy_path" not in public
    assert not any("learning" in key and "path" in key for key in public)
