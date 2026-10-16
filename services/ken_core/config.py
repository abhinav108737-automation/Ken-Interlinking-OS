"""Application configuration.

FROZEN CONTRACT (env var names). Every setting is read from the environment
with the ``KEN_`` prefix, so ``database_url`` comes from ``KEN_DATABASE_URL``.

Two settings carry architectural weight:

``require_qdrant`` / ``require_ollama``
    These flip a readiness probe between "failure makes us NOT_READY" and
    "failure merely degrades us". They are the entire mechanism by which one
    binary runs honestly both in native development and in the container
    stack. ``KEN_REQUIRE_PGVECTOR`` is GONE: ADR-0017 removed the vector
    extension and replaced its probe with a vector-store probe.

``autonomy_mode``
    ADR-0018. ``shadow`` by default, so installing this product changes
    nothing on a real site. ``autonomous`` is refused unless it is explicitly
    acknowledged, and refused against a write-enabled ``http`` CMS unless
    production is explicitly allowed. The process refuses to boot rather than
    publish by surprise.

``auto_approve_enabled``
    TRD.md section 20 requires AUTO_APPROVE to be globally disabled in v1. A
    validator raises at startup if it is ever set true, so the guarantee is
    enforced by the process refusing to boot rather than by a code review.
    ADR-0018 RETIRES the flag rather than reusing it: autonomy is expressed
    only by ``autonomy_mode``, so the two concepts cannot be confused.
"""

from __future__ import annotations

import functools
import ipaddress
import re
import subprocess
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
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


GA4_BASE_URL = "https://analyticsdata.googleapis.com"
GSC_BASE_URL = "https://www.googleapis.com"
GOOGLE_ANALYTICS_HOSTS: dict[str, str] = {
    "analytics_ga4_base_url": "analyticsdata.googleapis.com",
    "analytics_gsc_base_url": "www.googleapis.com",
}
"""The ONLY non-loopback host each Google base URL may name (ADR-0015 s12.3)."""


def is_loopback_host(host: str | None) -> bool:
    """Whether ``host`` is a loopback IP literal or ``localhost`` (no DNS lookup)."""
    if not host:
        return False
    name = host.strip().strip("[]").rstrip(".").lower()
    if name == "localhost":
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def parse_origin(value: str) -> str:
    """Normalise one exact web origin ``scheme://host[:port]`` (lower-case scheme and host).

    Raises ValueError for ``null``, a path, query, fragment, credentials, or a non-http(s)
    scheme: the collector compares origins EXACTLY (ADR-0015 s12.3)."""
    text = value.strip()
    parts = urlsplit(text)
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("an origin has an invalid port") from exc
    if (
        parts.scheme.lower() not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
        or text.endswith("?")
        or text.endswith("#")
    ):
        raise ValueError("an origin is scheme://host[:port] only")
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme.lower()}://{host}" + (f":{port}" if port is not None else "")


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
    phase: int = 3
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
    # Phase 6 (ADR-0014 s4/s8). The default stays the in-memory mock. "simulated" is the
    # local end-to-end CMS (its OWN database, outside the app's Alembic chain); "http" is the
    # production adapter for TRD section 13, inert until explicitly enabled: every call is
    # refused unless the base URL is loopback or KEN_CMS_ALLOW_REMOTE=true, and writes also
    # need KEN_CMS_WRITES_ENABLED=true. Pointing it at the real Ken CMS is an ASK action.
    cms_provider: Literal["mock", "simulated", "http"] = "mock"
    cms_sim_database_url: str = Field(
        default="postgresql+asyncpg://ken:ken@localhost:5433/ken_cms_sim",
        description="The simulated CMS's OWN database (project-local, disposable).",
    )
    cms_base_url: str | None = Field(default=None, description="HttpCMSProvider base URL.")
    cms_token: SecretStr | None = Field(
        default=None, description="HttpCMSProvider Bearer token. Never logged."
    )
    cms_writes_enabled: bool = Field(
        default=False, description="HttpCMSProvider: allow apply/rollback (ASK to enable)."
    )
    cms_allow_remote: bool = Field(
        default=False,
        description="HttpCMSProvider: allow a non-loopback base URL (ASK to enable).",
    )
    cms_timeout_s: float = Field(default=10.0, gt=0.0, le=120.0)

    # ------------------------------------------------------------- deployment
    # Phase 6. HTTP verification (published page + target reachability) runs only when a
    # verification base URL is set; the published URL of a page is this base + its path.
    deploy_verify_base_url: str | None = Field(
        default=None, description="Where published pages are served (HTTP verification)."
    )
    deploy_allowed_hosts: str = Field(
        default="",
        description="Comma-separated extra hosts an inserted href may point at (besides the "
        "source page's host and KEN_CRAWL_ALLOWED_HOSTS).",
    )
    deploy_verify_attempts: int = Field(default=3, ge=1, le=10)
    deploy_verify_retry_delay_s: float = Field(default=2.0, ge=0.0, le=60.0)
    deploy_http_timeout_s: float = Field(default=10.0, gt=0.0, le=60.0)
    deploy_reconcile_after_s: float = Field(
        default=300.0,
        ge=0.0,
        description="make deployments-reconcile resends rows applying/rolling_back this long.",
    )

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
    # Cap on materialised taxonomy edges per (source page, relationship type);
    # the top edges by (weight, shared geographies, target id) are kept. The
    # coarse types same_geography / regional_parent / industry_adjacency are
    # never materialised (Phase 3 derives them per candidate pair), so the
    # stored graph is bounded by pages x materialised types x this cap.
    relationship_max_per_type: int = Field(default=25, ge=1, le=500)

    # ---------------------------------------------------- recommendation engine
    # Phase 3. Thresholds, caps and compatibility matrices are versioned POLICY
    # DATA (ADR-0011); weights are the TRD section 12 ranking policy it references.
    recommendation_policy_path: str = Field(
        default=str(REPO_ROOT / "policies" / "recommendation" / "recommendation.v1.yaml"),
        description="A relative value is resolved against the repository root.",
    )

    # ------------------------------------------------------ agent supervisor
    # Phase 4. Model roles used, critic threshold, plans per run, timeouts and
    # retries are versioned POLICY DATA (ADR-0012).
    agents_policy_path: str = Field(
        default=str(REPO_ROOT / "policies" / "agents" / "agents.v1.yaml"),
        description="A relative value is resolved against the repository root.",
    )

    # -------------------------------------------------------------- analytics
    # Phase 7 (docs/adr/ADR-0015-analytics.md, section 12 binding). Windows, lags, bot
    # patterns and collector limits are versioned POLICY DATA. Reading stored analytics is
    # ALLOW; ANY Google egress is ASK: off by default, CLI-only, and when enabled the base
    # URLs must be https on the exact Google hosts (validated below). A loopback base URL is
    # a test fixture (local/ci only), for which the token file is never read.
    analytics_policy_path: str = Field(
        default=str(REPO_ROOT / "policies" / "analytics" / "analytics.v1.yaml"),
        description="A relative value is resolved against the repository root.",
    )
    analytics_google_enabled: bool = Field(
        default=False,
        description="Allow GA4 / Search Console API calls (external egress: ASK to enable).",
    )
    analytics_google_token_file: str | None = Field(
        default=None,
        description="File holding a read-only OAuth access token (mode 0600). Never logged, "
        "never in /meta, never in a job snapshot.",
    )
    ga4_property_id: str | None = Field(default=None, pattern=r"^[0-9]{1,20}$")
    ga4_timezone: str = Field(
        default="UTC", description="The GA4 property's time zone (its day boundaries)."
    )
    gsc_site_url: str | None = Field(
        default=None,
        max_length=2048,
        description="Search Console property: 'sc-domain:example.com' or a URL prefix.",
    )
    analytics_ga4_base_url: str = GA4_BASE_URL
    analytics_gsc_base_url: str = GSC_BASE_URL
    analytics_google_timeout_s: float = Field(default=30.0, gt=0.0, le=300.0)
    analytics_import_max_file_bytes: int = Field(
        default=50_000_000, ge=1, le=500_000_000, description="Largest CSV an import reads."
    )
    # The first-party collector (local simulation only until a production collector is
    # approved): a loopback IP literal at an OS-assigned port, never 8765.
    collector_host: str = Field(default="127.0.0.1", min_length=1, max_length=64)
    collector_allowed_origins: str = Field(
        default="",
        max_length=4096,
        description="Comma-separated exact origins (scheme://host[:port]); empty = the "
        "simulated site's own origin, set at startup.",
    )
    collector_allow_remote: bool = Field(
        default=False,
        description="Allow a non-loopback collector bind or origin (ASK to enable).",
    )

    # --------------------------------------------------------------- learning
    # Phase 8 (docs/adr/ADR-0016-learning.md, section 15 binding). Gate thresholds, the sample
    # floor, the required stage path per kind and the (narrowed) change allowlist are versioned
    # POLICY DATA. Reading learning data is ALLOW; advancing a proposal needs
    # Permission.PROMOTE (operator_admin only) -- and even that cannot promote while
    # demonstration mode is on.
    learning_policy_path: str = Field(
        default=str(REPO_ROOT / "policies" / "learning" / "learning.v1.yaml"),
        description="A relative value is resolved against the repository root.",
    )
    learning_demonstration_mode: bool = Field(
        default=True,
        description=(
            "DEFAULT TRUE, and deliberately so (ADR-0016 15.4 / 16.1). The corpus is ~65 "
            "SYNTHETIC pages, 48 plans and scripted review actions, so the one stream the "
            "learning design calls trustworthy -- human editorial decisions -- is the stream "
            "with no real data. While this is set, PromotionService.advance refuses "
            "to_status='promoted' with a typed DEMONSTRATION_MODE error and every /learning "
            "screen carries a banner naming the corpus size. Phase 8 therefore exits with "
            "zero promotions BY CONSTRUCTION rather than by promise. Turning it off is the "
            "user's decision and does not by itself make anything promotable: a passing "
            "evaluation bound to the current fingerprint is still required."
        ),
    )

    experiments_allow_holdout: bool = Field(
        default=False,
        description=(
            "Allow a HOLDOUT experiment to be ACTIVATED. Default false, and deliberately so: "
            "a holdout deliberately WITHHOLDS inbound links from its control pages, which is "
            "a real editorial and SEO cost on the client's live site. While this is unset, "
            "ExperimentLifecycle.activate refuses a holdout with a typed "
            "HOLDOUT_NOT_ENABLED error and does not set the transaction-local "
            "'ken.learning_experiment_activator' setting that migration 0009's guard requires "
            "-- so no code path, not even a raw UPDATE, can start one. The share of pages is "
            "capped at 20% by a database CHECK whatever this is set to, and a policy_ab "
            "experiment (both arms get links) is unaffected."
        ),
    )

    # ----------------------------------------------------------- vector store
    # ADR-0017: Qdrant is the derived, rebuildable semantic index; PostgreSQL keeps the
    # embedding VALUES and stays the source of truth. The extension is removed, not
    # bypassed: there is no KEN_REQUIRE_PGVECTOR and no extension created anywhere.
    vector_store: Literal["memory", "qdrant"] = Field(
        default="memory",
        description=(
            "DEFAULT 'memory' deliberately: the in-process exact-cosine store makes the "
            "whole test suite and CI run with no container. 'qdrant' is the local service."
        ),
    )
    qdrant_url: str = Field(
        default="http://127.0.0.1:6333",
        description="Base URL of the Qdrant HTTP API. Loopback unless staging/production.",
    )
    qdrant_collection: str = Field(
        default="ken_pages",
        min_length=1,
        max_length=200,
        pattern=r"^[a-z][a-z0-9_]{0,199}$",
        description="The one collection this system owns. Dropping it costs a reindex.",
    )
    qdrant_api_key: SecretStr | None = Field(
        default=None,
        description="Optional Qdrant API key. Never logged, never in /meta, never in a job "
        "snapshot (docs/CONTRACTS.md rule 9).",
    )
    qdrant_timeout_s: float = Field(default=10.0, gt=0.0, le=120.0)

    # -------------------------------------------------------------- readiness
    require_qdrant: bool = Field(
        default=True,
        description=(
            "ADR-0017 s5: the vector-store probe is REQUIRED while the semantic channel is "
            "enabled, which it is by default. With the default in-memory store the probe "
            "always answers ok, so this is honest rather than noisy; with "
            "KEN_VECTOR_STORE=qdrant an unreachable Qdrant correctly makes /ready 503."
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
    # Phase 9 (docs/adr/ADR-0018-autonomy-first-publishing.md section 2). There is no normal
    # human approval step any more, so the SAFETY lives here: the default mode publishes
    # nothing, and moving off it takes two deliberate acknowledgements.
    autonomy_mode: Literal["shadow", "guarded", "autonomous"] = Field(
        default="shadow",
        description=(
            "shadow (DEFAULT): evaluate and record what WOULD have happened; publish "
            "nothing. guarded: auto-publish only low-risk, high-confidence decisions. "
            "autonomous: publish every policy-eligible decision. Hard deterministic blocks "
            "apply in every mode and are never waived."
        ),
    )
    autonomy_autonomous_acknowledged: bool = Field(
        default=False,
        description=(
            "Required to select autonomous mode at all. Its only purpose is to make "
            "'publish without a human' an explicit, recorded operator act rather than the "
            "consequence of one environment variable."
        ),
    )
    autonomy_allow_production: bool = Field(
        default=False,
        description=(
            "Required before ANY publishing mode may run against a write-enabled 'http' CMS "
            "(the real site). ADR-0018 s2 names autonomous; guarded is included because it "
            "also publishes without a human, and a gate that only covers the louder mode is "
            "not a gate."
        ),
    )
    autonomy_verification_profile: Literal["standard", "strict"] = Field(
        default="standard",
        description="The profile used when the decision does not demand strict verification.",
    )
    autonomy_strict_verify_delay_s: float = Field(
        default=30.0,
        ge=0.0,
        le=3600.0,
        description="Strict profile: the pause before the SECOND verification pass.",
    )
    autonomy_strict_verify_attempts: int = Field(
        default=2, ge=2, le=10, description="Strict profile: at least two passes, by definition."
    )
    autonomy_publish_max_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
        description=(
            "ADR-0018 s4: the retry budget per (source, target). Once it is spent nothing "
            "republishes that pair, whatever the decision says."
        ),
    )
    autonomy_publish_cooldown_s: float = Field(
        default=3600.0,
        ge=0.0,
        le=30 * 86400.0,
        description="ADR-0018 s4: the cool-down after a failed publish or a rollback.",
    )

    auto_approve_enabled: bool = Field(
        default=False,
        description="RETIRED (ADR-0018 s2), not reused. Must remain false in v1 (TRD.md "
        "section 20); the validator below still refuses true.",
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

    @field_validator(
        "alembic_ini_path",
        "taxonomy_path",
        "recommendation_policy_path",
        "agents_policy_path",
        "analytics_policy_path",
        "learning_policy_path",
    )
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

    @model_validator(mode="after")
    def _autonomy_is_deliberate(self) -> Settings:
        """ADR-0018 s2. Two independent gates, both refusing at STARTUP.

        1. ``autonomous`` is never selected implicitly: it needs
           ``KEN_AUTONOMY_AUTONOMOUS_ACKNOWLEDGED=true`` as well.
        2. No publishing mode runs against a write-enabled ``http`` CMS -- the real site --
           without ``KEN_AUTONOMY_ALLOW_PRODUCTION=true``. ``guarded`` is covered too: it
           also publishes without a human.

        Refusing to boot is the point. A misconfiguration that would publish to a live site
        must not be discoverable only by looking at the site afterwards.
        """
        if self.autonomy_mode == "autonomous" and not self.autonomy_autonomous_acknowledged:
            raise ValueError(
                "KEN_AUTONOMY_MODE=autonomous requires "
                "KEN_AUTONOMY_AUTONOMOUS_ACKNOWLEDGED=true. Autonomous publishing writes to "
                "the CMS with no human in the loop; it is an explicit operator decision "
                "(ADR-0018 section 2), never an implicit one."
            )
        publishes = self.autonomy_mode in {"guarded", "autonomous"}
        production_cms = self.cms_provider == "http" and self.cms_writes_enabled
        if publishes and production_cms and not self.autonomy_allow_production:
            raise ValueError(
                f"KEN_AUTONOMY_MODE={self.autonomy_mode} is refused while the CMS provider is "
                "'http' with writes enabled, unless KEN_AUTONOMY_ALLOW_PRODUCTION=true. That "
                "combination publishes to the real site without a human (ADR-0018 section 2)."
            )
        return self

    @field_validator("qdrant_url")
    @classmethod
    def _qdrant_base_url(cls, value: str) -> str:
        """A bare ``scheme://host[:port]``, no credentials. ADR-0017: the service is local."""
        try:
            parts = urlsplit(value)
            _ = parts.port
        except ValueError as exc:
            raise ValueError("KEN_QDRANT_URL is not a valid URL") from exc
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("KEN_QDRANT_URL must be http(s)://host[:port]")
        if parts.username is not None or parts.password is not None:
            raise ValueError("KEN_QDRANT_URL must not carry credentials (use KEN_QDRANT_API_KEY)")
        if parts.query or parts.fragment or parts.path not in {"", "/"}:
            raise ValueError("KEN_QDRANT_URL must be a bare scheme://host[:port]")
        return value.rstrip("/")

    @model_validator(mode="after")
    def _qdrant_is_local_outside_deployment(self) -> Settings:
        """Compose binds Qdrant on 127.0.0.1 only. A non-loopback host in local or ci is a
        mistake, not a feature: it would send embeddings off this machine."""
        if self.environment in {"local", "ci"} and not is_loopback_host(
            urlsplit(self.qdrant_url).hostname
        ):
            # The compose service name is how a container reaches it inside the network.
            if urlsplit(self.qdrant_url).hostname != "qdrant":
                raise ValueError(
                    "KEN_QDRANT_URL must be loopback (or the 'qdrant' compose service) in a "
                    "local or ci environment"
                )
        return self

    @field_validator("ga4_timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("KEN_GA4_TIMEZONE is not a known IANA time zone") from exc
        return value

    @field_validator("gsc_site_url")
    @classmethod
    def _gsc_property(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value.startswith("sc-domain:"):
            if not re.fullmatch(r"sc-domain:[a-z0-9.-]{1,253}", value):
                raise ValueError("KEN_GSC_SITE_URL sc-domain property is malformed")
            return value
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.query:
            raise ValueError("KEN_GSC_SITE_URL must be 'sc-domain:<host>' or an http(s) prefix")
        return value

    @model_validator(mode="after")
    def _google_egress_is_guarded(self) -> Settings:
        """ADR-0015 s12.3: a Google base URL is the exact https Google host, or -- in local/ci
        only, and only while egress is disabled -- a loopback test fixture."""
        for field, expected_host in GOOGLE_ANALYTICS_HOSTS.items():
            value: str = getattr(self, field)
            env = field.upper()
            try:
                parts = urlsplit(value)
                _ = parts.port
            except ValueError as exc:
                raise ValueError(f"KEN_{env} is not a valid URL") from exc
            if parts.username is not None or parts.password is not None:
                raise ValueError(f"KEN_{env} must not carry credentials")
            if parts.query or parts.fragment or parts.path not in {"", "/"}:
                raise ValueError(f"KEN_{env} must be a bare scheme://host[:port]")
            if is_loopback_host(parts.hostname):
                if self.analytics_google_enabled:
                    raise ValueError(
                        f"KEN_{env} is a loopback fixture; it cannot be used with "
                        "KEN_ANALYTICS_GOOGLE_ENABLED=true"
                    )
                if self.environment not in {"local", "ci"}:
                    raise ValueError(f"KEN_{env} may be loopback only in local or ci")
                if parts.scheme not in {"http", "https"}:
                    raise ValueError(f"KEN_{env} must be http(s)")
                continue
            if parts.scheme != "https" or (parts.hostname or "").lower() != expected_host:
                raise ValueError(f"KEN_{env} must be https://{expected_host}")
            if parts.port not in {None, 443}:
                raise ValueError(f"KEN_{env} must use the default https port")
        if self.analytics_google_enabled and not self.analytics_google_token_file:
            raise ValueError(
                "KEN_ANALYTICS_GOOGLE_ENABLED=true requires KEN_ANALYTICS_GOOGLE_TOKEN_FILE"
            )
        return self

    @model_validator(mode="after")
    def _collector_is_local_unless_allowed(self) -> Settings:
        """The collector binds a loopback IP literal and accepts loopback origins only, unless
        KEN_COLLECTOR_ALLOW_REMOTE=true (ASK)."""
        try:
            address = ipaddress.ip_address(self.collector_host)
        except ValueError as exc:
            raise ValueError("KEN_COLLECTOR_HOST must be an IP literal") from exc
        if not address.is_loopback and not self.collector_allow_remote:
            raise ValueError(
                "KEN_COLLECTOR_HOST must be loopback unless KEN_COLLECTOR_ALLOW_REMOTE=true"
            )
        for origin in self.collector_origins():
            if not is_loopback_host(urlsplit(origin).hostname) and not self.collector_allow_remote:
                raise ValueError(
                    "KEN_COLLECTOR_ALLOWED_ORIGINS may name a non-loopback origin only with "
                    "KEN_COLLECTOR_ALLOW_REMOTE=true"
                )
        return self

    def collector_origins(self) -> tuple[str, ...]:
        """The configured exact origins, normalised (empty: none configured)."""
        try:
            return tuple(
                dict.fromkeys(
                    parse_origin(item)
                    for item in self.collector_allowed_origins.split(",")
                    if item.strip()
                )
            )
        except ValueError as exc:
            raise ValueError("KEN_COLLECTOR_ALLOWED_ORIGINS holds an invalid origin") from exc

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
            "require_qdrant": self.require_qdrant,
            "require_ollama": self.require_ollama,
            "vector_store": self.vector_store,
            "qdrant_collection": self.qdrant_collection,
            "autonomy_mode": self.autonomy_mode,
            "autonomy_autonomous_acknowledged": self.autonomy_autonomous_acknowledged,
            "autonomy_allow_production": self.autonomy_allow_production,
            "autonomy_verification_profile": self.autonomy_verification_profile,
            "crawl_host": crawl_host,
            "crawl_delay_s": self.crawl_delay_s,
            "crawl_concurrency": self.crawl_concurrency,
            "crawl_max_pages": self.crawl_max_pages,
            "analytics_google_enabled": self.analytics_google_enabled,
            "ga4_timezone": self.ga4_timezone,
            "collector_allow_remote": self.collector_allow_remote,
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
