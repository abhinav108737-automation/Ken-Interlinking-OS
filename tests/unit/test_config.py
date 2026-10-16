"""Settings: safety guards and the credential-free /meta allowlist."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ken_contracts.model import ModelRole
from ken_core.config import REPO_ROOT, ConfigurationError, Settings
from tests.conftest import make_settings

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


def test_defaults_are_safe() -> None:
    s = _settings()
    # ADR-0017: the vector-store probe replaced the pgvector one and is required; with the
    # default in-memory store it always answers ok. Ollama stays optional.
    assert s.require_qdrant is True
    assert not hasattr(s, "require_pgvector")
    assert s.vector_store == "memory"
    assert s.require_ollama is False
    assert s.auto_approve_enabled is False
    # ADR-0018: the default mode publishes NOTHING, and neither acknowledgement is set.
    assert s.autonomy_mode == "shadow"
    assert s.autonomy_autonomous_acknowledged is False
    assert s.autonomy_allow_production is False
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
        ("postgresql+asyncpg://app:real-secret@db.prod:5432/x", "production", False, False),
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


# --------------------------------------------------------------------------- #
# Phase 1: content inventory settings
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("environment", ["staging", "production"])
def test_default_credentials_are_refused_outside_development(environment: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        make_settings(environment=environment)
    assert "ken:ken" not in str(excinfo.value)


@pytest.mark.parametrize("environment", ["local", "ci"])
def test_default_credentials_are_fine_for_development(environment: str) -> None:
    assert make_settings(environment=environment).environment == environment


def test_crawl_has_no_default_site() -> None:
    """The crawler must never guess which site to crawl."""
    settings = make_settings()
    assert settings.crawl_base_url is None
    with pytest.raises(ConfigurationError, match="KEN_CRAWL_BASE_URL"):
        settings.crawl_config()


def test_crawl_config_is_built_from_settings() -> None:
    config = make_settings(
        crawl_base_url="https://example.com/start",
        crawl_allowed_hosts="WWW.example.com, cdn.example.com ,",
        crawl_delay_s=2.5,
        crawl_concurrency=3,
    ).crawl_config()
    assert config.base_url == "https://example.com/start"
    assert config.allowed_hosts == ("cdn.example.com", "www.example.com")
    assert (config.delay_s, config.concurrency) == (2.5, 3)


def test_crawl_politeness_defaults() -> None:
    settings = make_settings()
    assert settings.crawl_delay_s >= 1.0
    assert settings.crawl_concurrency <= 2
    assert settings.crawl_max_pages <= 1000


@pytest.mark.parametrize(
    "overrides",
    [
        {"crawl_concurrency": 0},
        {"crawl_concurrency": 9},
        {"crawl_delay_s": -1},
        {"crawl_max_redirects": 11},
    ],
)
def test_crawl_bounds_are_enforced(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make_settings(**overrides)


@pytest.mark.parametrize(
    "base_url",
    ["ftp://example.com/", "https://user:pw@example.com/", "not a url", "/relative"],
)
def test_crawl_base_url_must_be_plain_http(base_url: str) -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        make_settings(crawl_base_url=base_url).crawl_config()
    assert "pw" not in str(excinfo.value)


def test_public_settings_expose_crawl_host_only() -> None:
    public = make_settings(
        crawl_base_url="https://example.com/secret-path?token=x"
    ).public_settings()
    assert public["crawl_host"] == "example.com"
    assert "secret-path" not in repr(public)
    assert "token" not in repr(public)


# --------------------------------------------------------------------------- #
# Phase 9: the autonomy gates (docs/adr/ADR-0018-autonomy-first-publishing.md s2)
#
# There is no normal human approval step any more, so these validators ARE the safety.
# Each one makes the process refuse to BOOT rather than publish by surprise -- a
# misconfiguration that would write to a live site must not be discoverable only by
# looking at the site afterwards.
# --------------------------------------------------------------------------- #
def test_the_default_mode_publishes_nothing() -> None:
    assert _settings().autonomy_mode == "shadow"


@pytest.mark.parametrize("mode", ["shadow", "guarded"])
def test_shadow_and_guarded_need_no_acknowledgement(mode: str) -> None:
    assert _settings(autonomy_mode=mode).autonomy_mode == mode


def test_autonomous_is_refused_unless_separately_acknowledged() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _settings(autonomy_mode="autonomous")
    message = str(exc_info.value)
    assert "KEN_AUTONOMY_AUTONOMOUS_ACKNOWLEDGED" in message
    # Acknowledging it is enough on its own only while the CMS cannot write to a real site.
    settings = _settings(autonomy_mode="autonomous", autonomy_autonomous_acknowledged=True)
    assert settings.autonomy_mode == "autonomous"


def test_acknowledging_without_selecting_autonomous_changes_nothing() -> None:
    settings = _settings(autonomy_autonomous_acknowledged=True)
    assert settings.autonomy_mode == "shadow"


@pytest.mark.parametrize("mode", ["guarded", "autonomous"])
def test_a_publishing_mode_is_refused_against_a_write_enabled_http_cms(mode: str) -> None:
    overrides: dict[str, object] = {
        "autonomy_mode": mode,
        "autonomy_autonomous_acknowledged": True,
        "cms_provider": "http",
        "cms_writes_enabled": True,
        "cms_base_url": "http://127.0.0.1:9111",
    }
    with pytest.raises(ValidationError) as exc_info:
        _settings(**overrides)
    assert "KEN_AUTONOMY_ALLOW_PRODUCTION" in str(exc_info.value)
    # Explicitly allowing production is what makes it bootable, and nothing else.
    allowed = _settings(**overrides, autonomy_allow_production=True)
    assert allowed.autonomy_mode == mode


def test_a_publishing_mode_is_allowed_against_a_read_only_http_cms() -> None:
    """Writes disabled means the adapter refuses every mutation, so there is nothing to
    gate: the gate is on the combination that can actually change a live page."""
    settings = _settings(
        autonomy_mode="guarded",
        cms_provider="http",
        cms_writes_enabled=False,
        cms_base_url="http://127.0.0.1:9111",
    )
    assert settings.autonomy_mode == "guarded"


def test_shadow_is_allowed_even_against_a_write_enabled_production_cms() -> None:
    settings = _settings(
        autonomy_mode="shadow",
        cms_provider="http",
        cms_writes_enabled=True,
        cms_base_url="http://127.0.0.1:9111",
    )
    assert settings.autonomy_mode == "shadow"


def test_the_retired_auto_approve_flag_still_refuses_true() -> None:
    """ADR-0018 RETIRES the flag rather than reusing it, so the TRD section 20 guard stays."""
    with pytest.raises(ValidationError) as exc_info:
        _settings(auto_approve_enabled=True)
    assert "KEN_AUTO_APPROVE_ENABLED" in str(exc_info.value)
    assert _settings(autonomy_mode="guarded").auto_approve_enabled is False


def test_the_autonomy_settings_are_published_but_no_secret_is() -> None:
    public = _settings(qdrant_api_key=SECRET).public_settings()
    assert public["autonomy_mode"] == "shadow"
    assert public["autonomy_allow_production"] is False
    assert public["vector_store"] == "memory"
    assert public["qdrant_collection"] == "ken_pages"
    # The API key and the URL are deliberately absent from /meta (CONTRACTS rule 9).
    assert "qdrant_api_key" not in public
    assert "qdrant_url" not in public
    assert SECRET not in json.dumps(public)


# --------------------------------------------------------------------------- #
# Phase 9: the vector store (ADR-0017)
# --------------------------------------------------------------------------- #
def test_the_default_vector_store_needs_no_container() -> None:
    settings = _settings()
    assert settings.vector_store == "memory"
    assert settings.require_qdrant is True


@pytest.mark.parametrize(
    "url",
    [
        "ftp://127.0.0.1:6333",
        "http://user:pass@127.0.0.1:6333",
        "http://127.0.0.1:6333/collections",
        "http://127.0.0.1:6333?x=1",
        "not-a-url",
    ],
    ids=["scheme", "credentials", "path", "query", "garbage"],
)
def test_a_malformed_or_credential_bearing_qdrant_url_is_refused(url: str) -> None:
    with pytest.raises(ValidationError):
        _settings(qdrant_url=url)


def test_a_remote_qdrant_url_is_refused_locally() -> None:
    """Compose binds Qdrant on 127.0.0.1 only; a remote host would send embeddings off this
    machine. The compose SERVICE NAME is allowed, because that is how a container reaches it."""
    with pytest.raises(ValidationError):
        _settings(environment="local", qdrant_url="http://vectors.example.com:6333")
    assert _settings(environment="local", qdrant_url="http://qdrant:6333").qdrant_url
    # Outside local/ci the host is the deployment's business (and that environment needs
    # real database credentials, which is a separate guard).
    assert _settings(
        environment="production",
        qdrant_url="https://vectors.internal",
        database_url="postgresql+asyncpg://real:cred@db.internal/ken",
    ).qdrant_url


def test_the_qdrant_collection_name_is_constrained() -> None:
    with pytest.raises(ValidationError):
        _settings(qdrant_collection="Ken Pages")
    assert _settings(qdrant_collection="ken_pages_v2").qdrant_collection == "ken_pages_v2"
