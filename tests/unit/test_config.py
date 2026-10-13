"""Settings: safety guards and the credential-free /meta allowlist."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ken_contracts.model import ModelRole
from ken_core.config import REPO_ROOT, ConfigurationError, Settings

SECRET = "sup3rs3cret"


def _settings(**overrides: object) -> Settings:
    # _env_file=None: tests must not depend on a developer's local .env
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "url",
    [
        # password in the query string (libpq style) -- the originally reported leak
        f"postgresql+asyncpg://ken@db.internal:5432/ken_interlink?password={SECRET}&sslmode=verify-full",
        # password in userinfo, containing an encoded '@'
        f"postgresql+asyncpg://ken:{SECRET}%40x@db.internal:5432/ken_interlink",
        # unix-socket DSN with host and password both in the query string
        f"postgresql+asyncpg:///ken_interlink?host=/var/run/postgresql&password={SECRET}",
        # no port at all
        f"postgresql+asyncpg://ken:{SECRET}@db.internal/ken_interlink",
        # fragment present
        f"postgresql+asyncpg://ken:{SECRET}@db.internal:5432/ken_interlink#{SECRET}",
    ],
    ids=["query-password", "userinfo-encoded-at", "unix-socket", "no-port", "fragment"],
)
def test_public_settings_never_leaks_credentials(url: str) -> None:
    """Regression: GET /meta once published ?password= via naive string splitting."""
    dumped = json.dumps(_settings(database_url=url).public_settings())
    assert SECRET not in dumped
    assert "password" not in dumped.lower()


def test_public_settings_reports_host_port_and_database() -> None:
    public = _settings(
        database_url="postgresql+asyncpg://ken:ken@localhost:5433/ken_interlink"
    ).public_settings()
    assert public["database_host"] == "localhost"
    assert public["database_port"] == 5433
    assert public["database_name"] == "ken_interlink"


def test_auto_approve_cannot_be_enabled() -> None:
    """TRD.md section 20: AUTO_APPROVE is disabled globally in v1."""
    with pytest.raises(ValidationError, match="AUTO_APPROVE"):
        _settings(auto_approve_enabled=True)


def test_auto_approve_cannot_be_enabled_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEN_AUTO_APPROVE_ENABLED", "true")
    with pytest.raises(ValidationError, match="AUTO_APPROVE"):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://u:p@h/d",  # the previous driver (LGPL) -- now rejected
        "postgresql+psycopg2://u:p@h/d",
        "postgresql://u:p@h/d",
    ],
)
def test_only_the_asyncpg_driver_is_accepted(url: str) -> None:
    """ADR-0004: one driver (asyncpg, Apache-2.0), one URL scheme."""
    with pytest.raises(ValidationError, match="asyncpg"):
        _settings(database_url=url)


def test_driver_error_does_not_echo_credentials() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _settings(database_url=f"postgresql+psycopg://u:{SECRET}@h/d")  # a REJECTED driver
    assert SECRET not in str(exc_info.value)


def test_phase_0_defaults_are_lenient_and_safe() -> None:
    s = _settings()
    assert s.require_pgvector is False
    assert s.require_ollama is False
    assert s.auto_approve_enabled is False
    assert s.model_provider == "mock"
    assert ":5433/" in s.database_url  # the project container, never the host's 5432


@pytest.mark.parametrize("role", list(ModelRole))
def test_every_role_resolves_to_a_configured_model(role: ModelRole) -> None:
    assert _settings().role_model(role)


def test_unknown_role_raises_a_typed_error() -> None:
    with pytest.raises(ConfigurationError):
        _settings().role_model("not-a-role")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Regressions from the Phase 0 independent QA review
# --------------------------------------------------------------------------- #
def test_auto_approve_cannot_be_switched_on_after_startup() -> None:
    """P1: a validated invariant must not be bypassable by mutation at runtime."""
    s = _settings()
    with pytest.raises(ValidationError):
        s.auto_approve_enabled = True  # type: ignore[misc]
    assert s.auto_approve_enabled is False


def test_key_value_dsn_is_never_echoed_in_the_error() -> None:
    """M7: a libpq key=value DSN has no '://', so naive splitting echoed all of it."""
    with pytest.raises(ValidationError) as exc_info:
        _settings(database_url=f"host=db.internal user=ken password={SECRET} dbname=x")
    message = str(exc_info.value)
    assert SECRET not in message
    assert "password" not in message.lower()
    assert "non-URL value" in message


def test_alembic_ini_resolves_independently_of_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M2: launched from another directory, the migrations probe raised and /ready went 503."""
    monkeypatch.chdir(tmp_path)
    s = _settings()
    assert Path(s.alembic_ini_path).is_absolute()
    assert Path(s.alembic_ini_path).is_file()
    assert _settings(alembic_ini_path="alembic.ini").alembic_ini_path == s.alembic_ini_path


def test_env_file_location_is_independent_of_the_working_directory() -> None:
    env_file = Settings.model_config.get("env_file")
    assert isinstance(env_file, Path)
    assert env_file.is_absolute()
    assert env_file == REPO_ROOT / ".env"


@pytest.mark.parametrize(
    ("url", "environment", "allowed", "refused"),
    [
        ("postgresql+asyncpg://ken:ken@localhost:5432/x", "local", False, True),
        ("postgresql+asyncpg://ken:ken@localhost:5432/x", "local", True, False),
        ("postgresql+asyncpg://ken:ken@localhost:5433/x", "local", False, False),
        ("postgresql+asyncpg://ken:ken@db.prod:5432/x", "production", False, False),
        # Inside the compose network "postgres:5432" IS the project container.
        ("postgresql+asyncpg://ken:ken@postgres:5432/ken_interlink", "local", False, False),
        # A container reaching the HOST's own server through the gateway name.
        ("postgresql+asyncpg://ken:ken@host.docker.internal:5432/x", "local", False, True),
        ("postgresql+asyncpg://ken:ken@host.lima.internal:5432/x", "local", False, True),
        # No host and no port: libpq uses the local socket on 5432.
        ("postgresql+asyncpg:///ken_interlink", "local", False, True),
        ("postgresql+asyncpg://ken:ken@127.0.0.1/x", "local", False, True),
        ("postgresql+asyncpg://ken:ken@[::1]:5432/x", "local", False, True),
        # Bypasses found by the QA re-review -- all reach the local server:
        ("postgresql+asyncpg://ken:ken@0.0.0.0:5432/x", "local", False, True),
        ("postgresql+asyncpg://ken:ken@127.0.0.2:5432/x", "local", False, True),
        ("postgresql+asyncpg://ken:ken@localhost.:5432/x", "local", False, True),
        ("postgresql+asyncpg://ken:ken@[::ffff:127.0.0.1]:5432/x", "local", False, True),
        ("postgresql+asyncpg://ken:ken@localhost:5433/x?port=5432", "local", False, True),
        ("postgresql+asyncpg://ken:ken@postgres:5432/x?host=localhost", "local", False, True),
        ("postgresql+asyncpg://ken:ken@postgres:5432/x?host=/tmp", "local", False, True),
        # asyncpg returns LISTS for multi-host URLs; localhost:5432 in slot 2 must be caught.
        (
            "postgresql+asyncpg://ken:ken@h1:5433/x?host=h1:5433&host=localhost:5432",
            "local",
            False,
            True,
        ),
        # ...and a genuinely remote host on 5432 is still allowed.
        ("postgresql+asyncpg://ken:ken@10.0.0.5:5432/x", "local", False, False),
    ],
    ids=[
        "local-5432-refused",
        "local-5432-explicitly-allowed",
        "local-5433",
        "production-5432",
        "compose-service-name-allowed",
        "container-to-host-docker-refused",
        "container-to-host-colima-refused",
        "socket-dsn-default-port-refused",
        "loopback-default-port-refused",
        "ipv6-loopback-refused",
        "unspecified-0.0.0.0-refused",
        "loopback-127.0.0.2-refused",
        "trailing-dot-localhost-refused",
        "ipv4-mapped-loopback-refused",
        "query-port-override-refused",
        "query-host-override-refused",
        "query-socket-dir-refused",
        "multihost-second-slot-refused",
        "remote-host-5432-allowed",
    ],
)
def test_migrations_refuse_the_developers_own_postgres(
    url: str, environment: str, allowed: bool, refused: bool
) -> None:
    """M5: locally, 5432 is an unrelated pre-existing server; migrating it is destructive."""
    s = _settings(database_url=url, environment=environment, allow_native_postgres_port=allowed)
    if refused:
        with pytest.raises(ConfigurationError, match="5432"):
            s.assert_safe_migration_target()
    else:
        s.assert_safe_migration_target()
