"""The ONE publishing gate (``ken_services.autonomy.gate``) and its callers.

Before this module the rule lived in three places that could drift: the settings validator,
the engine's ``publishing_enabled()`` and the API reader's copy (which already disagreed with
the engine about a disabled-writes CMS). Now there is one function, tested as a table, and the
two duplicates are gone -- a static test asserts that, so they cannot quietly come back.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Final

import pytest

from ken_contracts.autonomy import AutonomyMode
from ken_core.config import REPO_ROOT
from ken_services.autonomy.engine import DatabaseAutonomyEngine
from ken_services.autonomy.gate import (
    PublishingNotEnabledError,
    PublishingRefusal,
    publishing_enabled,
    publishing_refusal,
    require_publishing,
)
from tests.unit.autonomy._support import settings

PACKAGE: Final = REPO_ROOT / "services" / "ken_services" / "autonomy"
R = PublishingRefusal


def _s(**overrides: Any) -> Any:
    """Settings built WITHOUT the validators, so every combination can be asked about --
    which is exactly the situation the runtime gate exists for."""
    return settings().model_copy(update=overrides)


CASES: list[tuple[dict[str, Any], AutonomyMode, PublishingRefusal | None]] = [
    ({}, AutonomyMode.SHADOW, R.SHADOW_MODE),
    # project-local providers are not "production": cms_writes_enabled gates only `http`
    ({}, AutonomyMode.GUARDED, None),
    ({"cms_provider": "simulated"}, AutonomyMode.GUARDED, None),
    ({}, AutonomyMode.AUTONOMOUS, R.AUTONOMOUS_NOT_ACKNOWLEDGED),
    ({"autonomy_autonomous_acknowledged": True}, AutonomyMode.AUTONOMOUS, None),
    # a real CMS: writes must be on AND production explicitly allowed
    ({"cms_provider": "http"}, AutonomyMode.GUARDED, R.CMS_WRITES_DISABLED),
    (
        {"cms_provider": "http", "cms_writes_enabled": True},
        AutonomyMode.GUARDED,
        R.PRODUCTION_NOT_ALLOWED,
    ),
    (
        {
            "cms_provider": "http",
            "cms_writes_enabled": True,
            "autonomy_autonomous_acknowledged": True,
        },
        AutonomyMode.AUTONOMOUS,
        R.PRODUCTION_NOT_ALLOWED,
    ),
    (
        {
            "cms_provider": "http",
            "cms_writes_enabled": True,
            "autonomy_allow_production": True,
        },
        AutonomyMode.AUTONOMOUS,
        R.AUTONOMOUS_NOT_ACKNOWLEDGED,
    ),
    (
        {
            "cms_provider": "http",
            "cms_writes_enabled": True,
            "autonomy_allow_production": True,
            "autonomy_autonomous_acknowledged": True,
        },
        AutonomyMode.AUTONOMOUS,
        None,
    ),
]


@pytest.mark.parametrize(
    ("overrides", "mode", "expected"),
    CASES,
    ids=[f"{m.value}-{e.value if e else 'allowed'}" for _o, m, e in CASES],
)
def test_the_gate_table(
    overrides: dict[str, Any], mode: AutonomyMode, expected: PublishingRefusal | None
) -> None:
    built = _s(**overrides)
    assert publishing_refusal(built, mode) is expected
    assert publishing_enabled(built, mode) is (expected is None)
    if expected is None:
        require_publishing(built, mode)
    else:
        with pytest.raises(PublishingNotEnabledError) as caught:
            require_publishing(built, mode)
        assert caught.value.refusal is expected
        assert expected.value in str(caught.value)


def test_the_default_installation_cannot_publish() -> None:
    """The configured mode is the default when none is named."""
    assert publishing_enabled(settings()) is False


def test_shadow_is_refused_whatever_else_is_configured() -> None:
    everything_on = _s(
        cms_provider="http",
        cms_writes_enabled=True,
        autonomy_allow_production=True,
        autonomy_autonomous_acknowledged=True,
    )
    assert publishing_refusal(everything_on, AutonomyMode.SHADOW) is R.SHADOW_MODE


def test_a_refusal_names_a_code_and_never_a_value() -> None:
    built = _s(cms_provider="http", cms_writes_enabled=True, cms_base_url="https://secret.test/x")
    with pytest.raises(PublishingNotEnabledError) as caught:
        require_publishing(built, AutonomyMode.GUARDED)
    assert "secret.test" not in str(caught.value)


def test_the_engine_no_longer_has_its_own_copy() -> None:
    assert not hasattr(DatabaseAutonomyEngine, "publishing_enabled")


def test_no_module_but_the_gate_reimplements_the_rule() -> None:
    """The rule is `cms_provider == "http"` crossed with `cms_writes_enabled` and
    `autonomy_allow_production`. Only ``gate.py`` may spell that combination."""
    offenders: list[str] = []
    for path in sorted(PACKAGE.glob("*.py")):
        if path.name == "gate.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        if {"cms_writes_enabled", "autonomy_allow_production"} <= attrs:
            offenders.append(path.name)
    # job.py and reader.py only REPORT these fields (a snapshot, a health payload); they never
    # branch on the pair. The settings validator in config.py is the boot-time layer.
    assert set(offenders) <= {"job.py", "reader.py"}, offenders


def test_the_reader_and_the_engine_import_the_one_function() -> None:
    reader = (PACKAGE / "reader.py").read_text(encoding="utf-8")
    assert "from ken_services.autonomy.gate import publishing_enabled" in reader
    assert "def _publishing_enabled" not in reader


def test_the_cli_maps_a_closed_gate_to_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from ken_services.autonomy import cli

    refusing = _s(autonomy_mode="autonomous", autonomy_autonomous_acknowledged=True)
    refusing = refusing.model_copy(update={"cms_provider": "http", "cms_writes_enabled": True})
    monkeypatch.setattr(cli, "get_settings", lambda: refusing)

    async def boom(*_a: object, **_k: object) -> Path:
        raise PublishingNotEnabledError(PublishingRefusal.PRODUCTION_NOT_ALLOWED)

    monkeypatch.setattr(cli, "_run", boom)
    assert cli.main(["evaluate", "--as-user", "otto"]) == cli.EXIT_USAGE
    err = capsys.readouterr().err
    assert "production_not_allowed" in err and "://" not in err
