"""Application configuration.

FROZEN CONTRACT (env var names). Every setting is read from the environment
with the ``KEN_`` prefix, so ``database_url`` comes from ``KEN_DATABASE_URL``.

Two settings carry architectural weight:

``require_pgvector`` / ``require_ollama``
    These flip a readiness probe between "failure makes us NOT_READY" and
    "failure merely degrades us". They are the entire mechanism by which one
    binary runs honestly both in native development (where pgvector is absent)
    and in the container stack (where it is present). See docs/adr/ADR-0003.md.

``auto_approve_enabled``
    TRD.md section 20 requires AUTO_APPROVE to be globally disabled in v1. A
    validator raises at startup if it is ever set true, so the guarantee is
    enforced by the process refusing to boot rather than by a code review.
"""

from __future__ import annotations

import functools
import ipaddress
import re
import subprocess
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ken_contracts.inventory import CrawlConfig
from ken_contracts.model import ModelRole

# services/ken_core/config.py -> repository root. Identical in native dev and in
# the container (/app), because both use an editable install of the source tree.
# Resolving paths from here, not the working directory, means the API behaves
# the same whatever directory it is launched from.
REPO_ROOT = Path(__file__).resolve().parents[2]

# The developer's OWN PostgreSQL listens here; the project database is :5433.
NATIVE_POSTGRES_PORT = 5432
# Names that reach THIS machine. The *.internal names are how a container
# reaches its host (Docker / Colima-Lima), so a container could otherwise
# migrate the developer's server. IP literals are classified by _is_host_local.
_HOST_LOCAL_NAMES = frozenset({"localhost", "host.docker.internal", "host.lima.internal"})


def _as_list(value: object) -> list[object]:
    """Normalise a dialect connect arg to a list.

    asyncpg's dialect returns LISTS for a libpq multi-host URL, where psycopg
    returned comma-separated strings. Treating a list as a string (e.g.
    ``str(value).split(",")``) yields fragments like ``"['h1'"`` and would let
    a local host hidden in the second slot slip past the guard.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [part.strip() for part in str(value).split(",")]


def _is_host_local(host: str) -> bool:
    """Whether a libpq host value reaches PostgreSQL on this machine.

    Covers: no host (local socket), a socket directory path, the local names
    above (including a trailing-dot FQDN form), all of 127.0.0.0/8, ::1,
    IPv4-mapped loopback, and the unspecified addresses 0.0.0.0 / ::, which
    connect to the local server.
    """
    name = host.strip().strip("[]").rstrip(".").lower()
    if not name or name.startswith("/"):
        return True
    if name in _HOST_LOCAL_NAMES:
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback or address.is_unspecified


_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]{0,31}")


class ConfigurationError(Exception):
    """Raised when configuration is internally inconsistent."""


class Settings(BaseSettings):
    """Runtime configuration, sourced from the environment and optional .env."""

    model_config = SettingsConfigDict(
        env_prefix="KEN_",
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # Settings inputs include credentials (KEN_DATABASE_URL). Without this,
        # pydantic appends `input_value='<the raw URL>'` to every validation
        # error, so a misconfigured URL would print its password into startup
        # crash logs. Covered by test_driver_error_does_not_echo_credentials.
        hide_input_in_errors=True,
        # Immutable after construction, so a validated invariant cannot be
        # switched off at runtime: `get_settings().auto_approve_enabled = True`
        # raises instead of silently bypassing the TRD section 20 guard.
        frozen=True,
    )

    # ---------------------------------------------------------------- service
    service_name: str = "ken-api"
    environment: Literal["local", "ci", "staging", "production"] = "local"
    phase: int = 2
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "console"

    # --------------------------------------------------------------- database
    # Default targets the Phase 0 project container on host port 5433, NOT the
    # developer's own PostgreSQL on 5432. See docs/adr/ADR-0003.md.
    database_url: str = "postgresql+asyncpg://ken:ken@localhost:5433/ken_interlink"
    database_echo: bool = False
    database_pool_size: int = Field(default=5, ge=1)
    database_pool_timeout_s: float = Field(default=10.0, gt=0)
    database_connect_timeout_s: float = Field(
        default=5.0,
        gt=0,
        description=(
            "asyncpg connect timeout. Its own default is 60 s, which let an unreachable "
            "database hang the worker for a minute, deaf to SIGTERM."
        ),
    )
    alembic_ini_path: str = Field(
        default=str(REPO_ROOT / "alembic.ini"),
        description="A relative value is resolved against the repository root, never the CWD.",
    )
    allow_native_postgres_port: bool = Field(
        default=False,
        description=(
            "Locally, port 5432 is the developer's own PostgreSQL holding unrelated data. "
            "Migrations refuse to run against it unless this is explicitly true."
        ),
    )

    # ----------------------------------------------------------- model server
    model_provider: Literal["mock", "ollama"] = "mock"
    ollama_base_url: str = "http://127.0.0.1:11434"
    model_fast: str = "qwen3:8b-q4_K_M"
    model_reasoning: str = "qwen3:8b-q4_K_M"
    # Intentionally unsatisfied in Phase 0: no embedding model is downloaded.
    # The readiness probe reports this role as missing and optional.
    model_embedding: str = "nomic-embed-text"
    mock_embedding_dim: int = Field(default=768, gt=0)

    # ------------------------------------------------------------------- cms
    cms_provider: Literal["mock"] = "mock"

    # ------------------------------------------------------- content inventory
    # Phase 1. There is deliberately NO default base URL: the crawler must never
    # guess which site to crawl. Until KEN_CRAWL_BASE_URL is set, `crawl` refuses
    # to start; file imports and tests work without it.
    crawl_base_url: str | None = Field(
        default=None, description="Start URL of the one site the crawler may visit."
    )
    crawl_allowed_hosts: str = Field(
        default="",
        description="Comma-separated extra in-scope hosts (e.g. the www. variant).",
    )
    crawl_user_agent: str = Field(
        default="KenInterlinkBot/0.1 (self-hosted content inventory; read-only)",
        min_length=1,
        max_length=200,
    )
    # Polite by default: one request start per second, two in flight at most.
    crawl_delay_s: float = Field(default=1.0, ge=0.0, le=60.0)
    crawl_concurrency: int = Field(default=2, ge=1, le=8)
    crawl_max_pages: int = Field(default=500, ge=1, le=100_000)
    crawl_max_depth: int = Field(default=10, ge=0, le=50)
    crawl_timeout_s: float = Field(default=15.0, gt=0.0, le=120.0)
    crawl_max_bytes: int = Field(default=5_000_000, ge=1024, le=50_000_000)
    crawl_max_redirects: int = Field(default=5, ge=0, le=10)
    ingest_max_file_bytes: int = Field(
        default=200_000_000, ge=1, description="Largest JSONL export an import will read."
    )

    # ---------------------------------------------------- content intelligence
    # Phase 2. The taxonomy is versioned POLICY DATA (ADR-0010).
    taxonomy_path: str = Field(
        default=str(REPO_ROOT / "policies" / "taxonomy" / "taxonomy.v1.yaml"),
        description="A relative value is resolved against the repository root.",
    )
    # Embedding text recipe. Prefixes are model conventions (configuration, not
    # business logic): the defaults suit the default local model,
    # nomic-embed-text, which expects task prefixes. Set both empty for a model
    # that does not use them.
    embedding_document_prefix: str = "search_document: "
    embedding_query_prefix: str = "search_query: "
    embedding_max_chars: int = Field(default=6000, ge=200, le=100_000)
    embedding_batch_size: int = Field(default=16, ge=1, le=256)
    similarity_top_k: int = Field(default=10, ge=1, le=100)
    # Model-specific: calibrated for nomic-embed-text on the demo corpus, where
    # related pairs average ~0.85 cosine and unrelated ~0.75 (evals/intelligence).
    # At 0.55 almost every pair qualified. Re-calibrate when the model changes;
    # the deterministic mock produces lower cosines (set ~0.3 for it).
    similarity_min_score: float = Field(default=0.78, ge=-1.0, le=1.0)

    # -------------------------------------------------------------- readiness
    require_pgvector: bool = Field(
        default=True,
        description=(
            "From Phase 2 true: migration 0003 creates the vector extension and the "
            "first vector column (ADR-0010). A server without pgvector can apply "
            "migrations up to 0002 only; the Phase 2 build then reports /ready 503 "
            "(migrations behind head) against it."
        ),
    )
    require_ollama: bool = Field(
        default=False,
        description="Phase 0 default false: no Phase 0 feature performs inference.",
    )
    readiness_timeout_ms: int = Field(default=2000, gt=0)

    # ---------------------------------------------------------------- worker
    worker_tick_seconds: float = Field(default=30.0, gt=0)

    # --------------------------------------------------------------- autonomy
    auto_approve_enabled: bool = Field(
        default=False,
        description="Must remain false in v1 (TRD.md section 20). Enforced below.",
    )

    @field_validator("database_url")
    @classmethod
    def _require_asyncpg_driver(cls, value: str) -> str:
        """Reject any driver other than asyncpg.

        One driver, async-only, Apache-2.0 (docs/adr/ADR-0004.md).
        Catching this here turns a confusing runtime import error into a clear
        configuration error.
        """
        if not value.startswith("postgresql+asyncpg://"):
            # Report the scheme only when one is clearly present. A libpq
            # key=value DSN ("host=... password=...") has no "://", and
            # value.split("://")[0] would then be the ENTIRE value, password
            # included -- hide_input_in_errors cannot help, because the text is
            # in the message itself.
            scheme, sep, _ = value.partition("://")
            shown = f"{scheme}://" if sep and _SCHEME_RE.fullmatch(scheme) else "a non-URL value"
            raise ValueError(
                "KEN_DATABASE_URL must use the 'postgresql+asyncpg://' scheme "
                f"(asyncpg is the only supported driver); got {shown} (value not shown)"
            )
        return value

    @field_validator("alembic_ini_path", "taxonomy_path")
    @classmethod
    def _resolve_against_repo_root(cls, value: str) -> str:
        path = Path(value)
        return str(path if path.is_absolute() else REPO_ROOT / path)

    @model_validator(mode="after")
    def _refuse_default_credentials_outside_dev(self) -> Settings:
        """The code-default ``ken:ken`` credentials are for local containers only."""
        if self.environment in {"staging", "production"}:
            parsed = urlsplit(self.database_url)
            if parsed.username == "ken" and parsed.password == "ken":  # noqa: S105
                raise ValueError(
                    "KEN_DATABASE_URL uses the local development credentials; set real "
                    "credentials for a staging or production environment"
                )
        return self

    @model_validator(mode="after")
    def _enforce_auto_approve_disabled(self) -> Settings:
        """TRD.md section 20: AUTO_APPROVE must be disabled globally in v1."""
        if self.auto_approve_enabled:
            raise ValueError(
                "KEN_AUTO_APPROVE_ENABLED must be false. TRD.md section 20 requires "
                "AUTO_APPROVE to be disabled globally in v1; enabling it is an ASK-level "
                "action requiring explicit human authorisation and a policy change."
            )
        return self

    def targets_native_postgres(self) -> bool:
        """True when ANY connection target is the developer's own PostgreSQL.

        That means port 5432 on this machine. The target is resolved the way the
        driver will actually connect -- via SQLAlchemy's asyncpg dialect -- so
        query-string overrides (``?port=5432``, ``?host=localhost``) and libpq
        multi-host lists (``?host=a:5433&host=localhost:5432``) cannot slip past
        a check of the URL's visible host:port.

        Deliberately NOT matched: ``postgres:5432`` inside the compose network,
        where ``postgres`` is the project container reached by service name.
        """
        from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg
        from sqlalchemy.engine import make_url

        try:
            # SQLAlchemy ships these dialect internals without annotations; the
            # ignore is scoped to this one call and to that one error code.
            dialect = PGDialect_asyncpg()  # type: ignore[no-untyped-call]
            _, kwargs = dialect.create_connect_args(  # type: ignore[no-untyped-call]
                make_url(self.database_url)
            )
        except Exception:  # an unparseable URL fails elsewhere; err on the safe side here
            return True
        hosts = _as_list(kwargs.get("host"))
        ports = _as_list(kwargs.get("port"))
        for index, host in enumerate(hosts or [""]):
            raw_port = ports[index] if index < len(ports) else (ports[0] if ports else "")
            try:
                text_port = str(raw_port).strip()
                port = int(text_port) if text_port else NATIVE_POSTGRES_PORT  # libpq default
            except ValueError:
                return True
            if port == NATIVE_POSTGRES_PORT and _is_host_local(str(host)):
                return True
        return False

    def assert_safe_migration_target(self) -> None:
        """Refuse to migrate the developer's own PostgreSQL by accident.

        Locally, port 5432 on this machine is an unrelated pre-existing server.
        A mistyped KEN_DATABASE_URL would otherwise create alembic_version and
        events in someone else's database before any warning appeared. Called
        by migrations/env.py before any DDL runs.
        """
        if (
            self.environment == "local"
            and self.targets_native_postgres()
            and not self.allow_native_postgres_port
        ):
            raise ConfigurationError(
                "Refusing to run migrations against PostgreSQL on this machine's port 5432 "
                "in the local environment: that is the developer's own server, not the "
                "project database (127.0.0.1:5433, or postgres:5432 inside compose). Fix "
                "KEN_DATABASE_URL, or set KEN_ALLOW_NATIVE_POSTGRES_PORT=true if intended."
            )

    def crawl_config(self) -> CrawlConfig:
        """The crawl configuration, or ConfigurationError when no site is configured.

        Never falls back to a guessed URL (Phase 1 brief: a real crawl awaits a
        user-configured source URL).
        """
        if not self.crawl_base_url:
            raise ConfigurationError(
                "KEN_CRAWL_BASE_URL is not set. The crawler never guesses a site: set it "
                "to the one site you have approved for crawling."
            )
        try:
            return CrawlConfig(
                base_url=self.crawl_base_url,
                allowed_hosts=tuple(
                    host for host in self.crawl_allowed_hosts.split(",") if host.strip()
                ),
                user_agent=self.crawl_user_agent,
                delay_s=self.crawl_delay_s,
                concurrency=self.crawl_concurrency,
                max_pages=self.crawl_max_pages,
                max_depth=self.crawl_max_depth,
                timeout_s=self.crawl_timeout_s,
                max_bytes=self.crawl_max_bytes,
                max_redirects=self.crawl_max_redirects,
            )
        except ValueError as exc:
            # The base URL is not secret, but keep the message generic anyway.
            raise ConfigurationError(
                "KEN_CRAWL_BASE_URL / KEN_CRAWL_* settings are invalid"
            ) from exc

    def role_model(self, role: ModelRole) -> str:
        """Resolve a model role to its configured model name.

        Typed rather than stringly-typed: a bare dict lookup on ``str`` turned
        a typo into a ``KeyError`` deep inside a provider.
        """
        mapping = {
            ModelRole.FAST: self.model_fast,
            ModelRole.REASONING: self.model_reasoning,
            ModelRole.EMBEDDING: self.model_embedding,
        }
        try:
            return mapping[ModelRole(role)]
        except ValueError as exc:
            raise ConfigurationError(f"Unknown model role: {role!r}") from exc

    def public_settings(self) -> dict[str, object]:
        """Non-secret settings allowlist for ``GET /meta``.

        An allowlist, never a dump. The database URL is reduced to host, port
        and database name using a real URL parser: string splitting is not
        safe here because a libpq-style DSN may legitimately carry credentials
        in its query string (``?password=...``), and naive partitioning would
        publish them over HTTP.
        """
        host: str = "unknown"
        port: int | str | None = None
        database = "unknown"
        try:
            parsed = urlsplit(self.database_url)
            # .hostname and .port exclude userinfo entirely, so credentials
            # cannot survive into the payload.
            host = parsed.hostname or "unknown"
            port = parsed.port
            # .path is the database name only; the query string is discarded,
            # which is what keeps ?password= out of the response.
            database = parsed.path.lstrip("/") or "unknown"
        except ValueError:  # pragma: no cover - malformed URL
            pass
        crawl_host: str | None = None
        if self.crawl_base_url:
            try:
                crawl_host = urlsplit(self.crawl_base_url).hostname
            except ValueError:  # pragma: no cover - malformed URL
                crawl_host = None
        return {
            "environment": self.environment,
            "phase": self.phase,
            "log_level": self.log_level,
            "model_provider": self.model_provider,
            "cms_provider": self.cms_provider,
            "database_host": host,
            "database_port": port,
            "database_name": database,
            "require_pgvector": self.require_pgvector,
            "require_ollama": self.require_ollama,
            "crawl_host": crawl_host,
            "crawl_delay_s": self.crawl_delay_s,
            "crawl_concurrency": self.crawl_concurrency,
            "crawl_max_pages": self.crawl_max_pages,
            "model_roles": {
                "fast": self.model_fast,
                "reasoning": self.model_reasoning,
                "embedding": self.model_embedding,
            },
        }


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton. Cached so .env is read once."""
    return Settings()


@functools.lru_cache(maxsize=1)
def git_sha() -> str | None:
    """Short commit SHA, or None when git is unavailable.

    Best-effort only: a missing SHA must never prevent the service starting.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None
