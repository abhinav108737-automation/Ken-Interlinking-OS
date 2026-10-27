"""Safe, read-only public-site crawl pilots: configuration, preflight and the gated crawl.

    python -m ken_services.inventory.pilot preflight --config pilots/kenresearch/pilot-25.yaml
    python -m ken_services.inventory.pilot preflight --config ... --live-robots --approval FILE
    python -m ken_services.inventory.pilot crawl     --config ... --approval FILE

``preflight`` (default) performs NO network operation to the site: it validates the version-
controlled pilot file against hard ceilings, shows what a run WOULD do, checks the local destination
database and lists blockers. ``--live-robots`` is a separate, approval-gated network operation that
fetches ONLY ``/robots.txt`` of the approved hosts. ``crawl`` is the only command that fetches pages
and it refuses to start without (a) no blockers, (b) an ``activated`` pilot and (c) an operator
approval file whose hash matches the exact pilot configuration. No approval file is shipped.

Hard ceilings (a pilot file that exceeds them fails to load): one request in flight, at least 3 s
between request starts, at most 500 page requests, https only, robots.txt required (a missing or
forbidden robots.txt fails closed), back-off enabled, explicit approved hosts, shadow autonomy mode.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from ken_contracts.common import FrozenModel
from ken_contracts.inventory import CrawlConfig
from ken_core import logging as ken_logging
from ken_core.config import ConfigurationError, Settings, get_settings
from ken_services.inventory import urls
from ken_services.inventory.robots import ROBOTS_PATH
from ken_services.inventory.sources.public_web import rule_reason

__all__ = ["PilotConfig", "load_pilot", "main", "pilot_hash", "preflight"]

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
MIN_DELAY_S: Final = 3.0
MAX_PILOT_PAGES: Final = 500
MAX_ROBOTS_REDIRECTS: Final = 5
SYNTHETIC_DATABASES: Final = frozenset({"ken_interlink", "ken_demo", "ken_cms_sim", "ken_pilot"})
_HOST_RE: Final = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_PLACEHOLDER_CONTACTS: Final = ("example", "changeme", "change_me", "todo", "xxx", "invalid")

EXIT_OK: Final = 0
EXIT_BLOCKED: Final = 1
EXIT_USAGE: Final = 2


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
class _Site(FrozenModel):
    base_url: str
    approved_hosts: tuple[str, ...] = Field(min_length=1)
    sitemap_sources: tuple[str, ...] = ()

    @field_validator("approved_hosts")
    @classmethod
    def _hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        out: list[str] = []
        for host in value:
            lowered = host.strip().lower()
            if not _HOST_RE.match(lowered):
                raise ValueError(
                    "approved_hosts entries must be plain hostnames (no scheme, port or wildcard)"
                )
            out.append(lowered)
        if len(set(out)) != len(out):
            raise ValueError("approved_hosts must not repeat a host")
        return tuple(out)

    @field_validator("sitemap_sources")
    @classmethod
    def _no_sitemaps(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value:
            raise ValueError(
                "sitemap_sources must be empty: the crawler has no sitemap reader, so no sitemap "
                "may widen the host, robots or request limits"
            )
        return value


class _Identity(FrozenModel):
    product: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{2,40}/[0-9][0-9A-Za-z.]{0,10}$")
    contact_env: str = Field(pattern=r"^KEN_[A-Z0-9_]{3,60}$")


class _Limits(FrozenModel):
    max_pages: int = Field(ge=1, le=MAX_PILOT_PAGES)
    concurrency: int = Field(ge=1, le=1)
    delay_s: float = Field(ge=MIN_DELAY_S, le=60.0)
    max_depth: int = Field(ge=0, le=6)
    timeout_s: float = Field(gt=0.0, le=60.0)
    max_bytes: int = Field(ge=1024, le=10_000_000)
    max_redirects: int = Field(ge=0, le=5)
    max_query_variants_per_path: int = Field(ge=1, le=10)


class _Politeness(FrozenModel):
    render: Literal["http", "browser"] = "http"
    browser_max_subrequests: int = Field(default=15, ge=0, le=30)
    https_only: Literal[True]
    require_robots_file: Literal[True]
    max_consecutive_blocks: int = Field(ge=1, le=10)
    max_retry_after_s: float = Field(gt=0.0, le=3600.0)


class _Rules(FrozenModel):
    include_path_prefixes: tuple[str, ...] = Field(min_length=1)
    exclude_path_globs: tuple[str, ...] = ()
    exclude_query_params: tuple[str, ...] = ()
    exclude_extensions: tuple[str, ...] = ()


class _Destination(FrozenModel):
    database: str = Field(pattern=r"^ken_[a-z0-9_]{3,50}$")
    qdrant_collection: str = Field(pattern=r"^ken_[a-z0-9_]{3,100}$")

    @model_validator(mode="after")
    def _separate_from_synthetic(self) -> _Destination:
        if self.database in SYNTHETIC_DATABASES or self.database.endswith("_test"):
            raise ValueError(
                "the destination database must not be a synthetic, demo or test database"
            )
        if self.qdrant_collection in {"ken_pages"} or "demo" in self.qdrant_collection:
            raise ValueError(
                "the destination collection must not be a synthetic or demo collection"
            )
        return self


class PilotConfig(FrozenModel):
    schema_version: Literal[1] = Field(alias="schema")
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,60}$")
    activated: bool
    description: str = Field(min_length=1, max_length=2000)
    site: _Site
    identity: _Identity
    limits: _Limits
    politeness: _Politeness
    rules: _Rules
    destination: _Destination
    autonomy_mode: Literal["shadow"]

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    @model_validator(mode="after")
    def _base_host_is_approved(self) -> PilotConfig:
        parts = urlsplit(self.site.base_url)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("site.base_url must be an https URL")
        if parts.username or parts.password or parts.port not in (None, 443):
            raise ValueError("site.base_url must carry no credentials and no custom port")
        if parts.hostname.lower() not in self.site.approved_hosts:
            raise ValueError("the base URL host must be in approved_hosts")
        return self

    # ---- derived ------------------------------------------------------------------
    def user_agent(self, contact: str) -> str:
        link = f"mailto:{contact}" if "@" in contact else contact
        return f"{self.identity.product} (+{link}; read-only content inventory)"[:200]

    def crawl_config(self, contact: str) -> CrawlConfig:
        base_host = (urlsplit(self.site.base_url).hostname or "").lower()
        return CrawlConfig(
            base_url=self.site.base_url,
            allowed_hosts=tuple(h for h in self.site.approved_hosts if h != base_host),
            user_agent=self.user_agent(contact),
            delay_s=self.limits.delay_s,
            concurrency=self.limits.concurrency,
            max_pages=self.limits.max_pages,
            max_depth=self.limits.max_depth,
            timeout_s=self.limits.timeout_s,
            max_bytes=self.limits.max_bytes,
            max_redirects=self.limits.max_redirects,
            max_query_variants_per_path=self.limits.max_query_variants_per_path,
            https_only=True,
            require_robots_file=True,
            include_path_prefixes=self.rules.include_path_prefixes,
            exclude_path_globs=self.rules.exclude_path_globs,
            exclude_query_params=self.rules.exclude_query_params,
            exclude_extensions=self.rules.exclude_extensions,
            render=self.politeness.render,
            browser_max_subrequests=self.politeness.browser_max_subrequests,
            max_consecutive_blocks=self.politeness.max_consecutive_blocks,
            max_retry_after_s=self.politeness.max_retry_after_s,
        )

    def request_ceiling(self) -> dict[str, int | float]:
        """Worst-case HTTP requests and the shortest time they need at the configured spacing."""
        limits = self.limits
        per_page = limits.max_redirects + 1
        if self.politeness.render == "browser":  # plus the same-host scripts a page may load
            per_page += self.politeness.browser_max_subrequests
        pages = limits.max_pages * per_page
        robots = len(self.site.approved_hosts) * (MAX_ROBOTS_REDIRECTS + 1)
        total = pages + robots
        return {
            "page_requests_typical_max": limits.max_pages,
            "http_requests_worst_case": total,
            "robots_requests_worst_case": robots,
            "minimum_seconds_worst_case": round(total * limits.delay_s, 1),
            "minimum_seconds_typical": round((limits.max_pages + 1) * limits.delay_s, 1),
        }


def load_pilot(path: Path) -> PilotConfig:
    """Parse and validate a pilot file. Raises ``ValueError`` (message safe to print)."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read the pilot file ({type(exc).__name__})") from exc
    if not isinstance(raw, dict):
        raise ValueError("the pilot file must be a YAML mapping")
    try:
        return PilotConfig.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors(include_url=False, include_input=False)[0]
        where = ".".join(str(p) for p in first["loc"])
        raise ValueError(f"invalid pilot configuration at {where}: {first['msg']}") from exc


def pilot_hash(config: PilotConfig) -> str:
    """SHA-256 of the canonical configuration: what an approval file must quote."""
    canonical = json.dumps(config.model_dump(mode="json", by_alias=True), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Approval
# --------------------------------------------------------------------------- #
def read_approval(path: Path | None, config: PilotConfig, scope: str) -> tuple[bool, str]:
    """Whether ``path`` is a valid, unexpired approval for ``scope`` of THIS configuration."""
    if path is None:
        return False, "no approval file supplied"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "approval file is missing or is not valid JSON"
    if not isinstance(data, dict):
        return False, "approval file must be a JSON object"
    if data.get("config_sha256") != pilot_hash(config):
        return False, "approval does not match this exact pilot configuration (hash differs)"
    if scope not in (data.get("scopes") or []):
        return False, f"approval does not grant the {scope!r} scope"
    if sorted(data.get("hosts") or []) != sorted(config.site.approved_hosts):
        return False, "approval hosts differ from the configuration's approved_hosts"
    if not str(data.get("approved_by", "")).strip():
        return False, "approval has no approved_by"
    try:
        expires = datetime.fromisoformat(str(data["expires_at"]))
    except (KeyError, ValueError):
        return False, "approval has no valid expires_at"
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires <= datetime.now(UTC):
        return False, "approval has expired"
    return True, "approved"


# --------------------------------------------------------------------------- #
# Preflight (offline)
# --------------------------------------------------------------------------- #
def _contact(config: PilotConfig, environ: dict[str, str]) -> tuple[str | None, str | None]:
    value = environ.get(config.identity.contact_env, "").strip()
    if not value:
        return None, f"{config.identity.contact_env} is not set (a role mailbox or a public URL)"
    lowered = value.lower()
    if any(token in lowered for token in _PLACEHOLDER_CONTACTS):
        return None, f"{config.identity.contact_env} is a placeholder"
    if "@" not in value and not value.startswith("https://"):
        return None, f"{config.identity.contact_env} must be an email address or an https URL"
    return value, None


def destination_url(settings: Settings, config: PilotConfig) -> str:
    """The configured application DSN pointed at the pilot database (same server and role)."""
    return (
        make_url(settings.database_url)
        .set(database=config.destination.database)
        .render_as_string(hide_password=False)
    )


async def inspect_destination(settings: Settings, config: PilotConfig) -> dict[str, Any]:
    """Local database facts only (no site traffic): reachable? migrated? what is already in it?"""
    url = make_url(destination_url(settings, config))
    info: dict[str, Any] = {
        "database": config.destination.database,
        "server": f"{url.host}:{url.port}",
        "reachable": False,
        "migrated": None,
        "pages": None,
        "hosts_present": [],
    }
    engine = create_async_engine(url, poolclass=NullPool, connect_args={"timeout": 5})
    try:
        async with engine.connect() as connection:
            info["reachable"] = True
            version = (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one_or_none()
            info["migrated"] = version
            info["pages"] = (
                await connection.execute(text("SELECT count(*) FROM pages"))
            ).scalar_one()
            rows = await connection.execute(
                text("SELECT DISTINCT split_part(canonical_url, '/', 3) FROM pages ORDER BY 1")
            )
            info["hosts_present"] = [r[0] for r in rows]
    except Exception as exc:  # never exception text: it can carry a DSN
        info["error"] = type(exc).__name__
    finally:
        await engine.dispose()
    return info


def _code_head() -> str | None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    ini = REPO_ROOT / "alembic.ini"
    return ScriptDirectory.from_config(Config(str(ini))).get_current_head()


async def preflight(
    config: PilotConfig,
    settings: Settings,
    *,
    environ: dict[str, str],
    approval: Path | None = None,
    inspect_db: bool = True,
) -> dict[str, Any]:
    """The offline report. Opens no connection to the target site."""
    blockers: list[str] = []
    contact, contact_problem = _contact(config, environ)
    if contact_problem:
        blockers.append(contact_problem)
    if not config.activated:
        blockers.append("pilot is not activated (activated: false)")
    if settings.autonomy_mode != "shadow":
        blockers.append(
            f"KEN_AUTONOMY_MODE is {settings.autonomy_mode!r}; a pilot requires 'shadow'"
        )
    if settings.cms_provider != "mock":
        blockers.append(f"KEN_CMS_PROVIDER is {settings.cms_provider!r}; a pilot requires 'mock'")
    if settings.vector_store != "qdrant":
        blockers.append("KEN_VECTOR_STORE is not 'qdrant'")
    if config.politeness.render == "browser":
        missing = _browser_problem()
        if missing:
            blockers.append(missing)
    seed = urls.normalize_url(config.site.base_url)
    probe = config.crawl_config(contact or "placeholder@invalid.invalid")
    # the seed must pass its own rules
    seed_reason = rule_reason(probe, seed)
    if seed_reason:
        blockers.append(f"the base URL is refused by the crawl rules ({seed_reason})")
    database: dict[str, Any] = {"database": config.destination.database}
    if inspect_db:
        database = await inspect_destination(settings, config)
        if not database["reachable"]:
            blockers.append(
                f"destination database {config.destination.database!r} is not reachable "
                "(create it locally: make pilot-db-create)"
            )
        else:
            head = _code_head()
            if database["migrated"] != head:
                blockers.append(
                    f"destination database is at {database['migrated']!r}, code head is {head!r}"
                )
            foreign = [h for h in database["hosts_present"] if h not in config.site.approved_hosts]
            if foreign:
                blockers.append(
                    "destination database already holds pages from other hosts "
                    f"({', '.join(foreign)}); live content must be stored separately"
                )
    approved_robots, robots_note = read_approval(approval, config, "live_robots_check")
    approved_crawl, crawl_note = read_approval(approval, config, "live_crawl")
    ceiling = config.request_ceiling()
    return {
        "pilot": config.name,
        "config_sha256": pilot_hash(config),
        "activated": config.activated,
        "mode": "offline preflight: no request was sent to the target site",
        "effective_allowed_hosts": list(config.site.approved_hosts),
        "not_approved": _not_approved(config),
        "base_url": config.site.base_url,
        "user_agent": config.user_agent(contact) if contact else None,
        "robots": {
            "policy": "required: robots.txt must answer 2xx, otherwise the origin is disallowed",
            "status": "NOT CHECKED (live check needs approval: --live-robots)",
            "live_check_approval": robots_note if not approved_robots else "approved",
        },
        "sitemap_sources": "none; the crawler has no sitemap reader (link-following only)",
        "render": config.politeness.render,
        "limits": config.limits.model_dump(),
        "politeness": config.politeness.model_dump(),
        "rules": config.rules.model_dump(),
        "request_ceiling": ceiling,
        "destination": database,
        "qdrant_collection": config.destination.qdrant_collection,
        "autonomy_mode": settings.autonomy_mode,
        "cms_provider": settings.cms_provider,
        "approvals": {
            "live_robots_check": "granted" if approved_robots else f"required ({robots_note})",
            "live_crawl": "granted" if approved_crawl else f"required ({crawl_note})",
        },
        "blockers": blockers,
    }


def _browser_problem() -> str | None:
    """Why browser rendering cannot start here. Looks at the filesystem only: it starts nothing
    and opens no socket (the Playwright driver must not run inside the preflight)."""
    import importlib.util
    import os
    import sys

    if importlib.util.find_spec("playwright") is None:
        return "render is 'browser' but Playwright is not installed (pip install 'playwright')"
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override:
        root = Path(override)
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Caches" / "ms-playwright"
    else:
        root = Path.home() / ".cache" / "ms-playwright"
    if root.is_dir() and any(root.glob("chromium*")):
        return None
    return "render is 'browser' but Chromium is missing (python -m playwright install chromium)"


def _not_approved(config: PilotConfig) -> list[str]:
    """Hosts a reader might assume are covered but are not (www vs apex)."""
    out: list[str] = []
    for host in config.site.approved_hosts:
        twin = host[4:] if host.startswith("www.") else f"www.{host}"
        if twin not in config.site.approved_hosts:
            out.append(f"{twin} (separate host; not approved)")
    return out


# --------------------------------------------------------------------------- #
# Live operations (approval-gated)
# --------------------------------------------------------------------------- #
async def live_robots(config: PilotConfig, contact: str) -> list[dict[str, Any]]:
    """Fetch ONLY /robots.txt of each approved host, with the pilot's guard, UA and spacing."""
    from ken_services.inventory.sources.public_web import PublicWebContentSource

    crawl = config.crawl_config(contact)
    source = PublicWebContentSource(crawl)
    report: list[dict[str, Any]] = []
    try:
        for host in config.site.approved_hosts:
            origin = f"https://{host}"
            policy, issue = await source.robots_status(origin)
            path = urlsplit(config.site.base_url).path or "/"
            report.append(
                {
                    "host": host,
                    "robots_url": origin + ROBOTS_PATH,
                    "available": issue is None,
                    "problem": None if issue is None else f"{issue.code.value}: {issue.message}",
                    "crawl_delay": policy.crawl_delay,
                    "base_path_allowed": policy.can_fetch(path),
                }
            )
    finally:
        await source.aclose()
    return report


async def run_crawl(
    config: PilotConfig, settings: Settings, contact: str, screenshots: Path | None = None
) -> dict[str, Any]:
    from ken_core.db.engine import create_engine
    from ken_core.db.session import create_sessionmaker
    from ken_services.inventory.ingest import run_ingestion
    from ken_services.inventory.sources.public_web import PublicWebContentSource

    pilot_settings = settings.model_copy(update={"database_url": destination_url(settings, config)})
    engine = create_engine(pilot_settings)
    source = PublicWebContentSource(config.crawl_config(contact), screenshot_dir=screenshots)
    try:
        detail = await run_ingestion(source=source, sessionmaker=create_sessionmaker(engine))
    finally:
        await engine.dispose()
    return {
        "run_id": str(detail.id),
        "status": detail.status.value,
        "counters": detail.counters.model_dump(),
        "failure_reason": detail.failure_reason,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ken_services.inventory.pilot")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, helptext in (
        ("preflight", "offline report; add --live-robots for the approved robots.txt check"),
        ("crawl", "run the approved pilot crawl (refuses without an approval file)"),
    ):
        command = sub.add_parser(name, help=helptext)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--approval", type=Path, default=None)
        if name == "crawl":
            command.add_argument(
                "--screenshots", type=Path, default=None, help="save a full-page PNG per page here"
            )
        if name == "preflight":
            command.add_argument("--live-robots", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code not in (0, None) else EXIT_OK
    try:
        config = load_pilot(args.config if args.config.is_absolute() else REPO_ROOT / args.config)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    try:
        settings = get_settings()
    except (ValidationError, ConfigurationError):
        print("configuration error: invalid KEN_* settings", file=sys.stderr)
        return EXIT_USAGE
    ken_logging.configure_logging(level=settings.log_level, fmt=settings.log_format)
    import os

    environ = dict(os.environ)
    approval = args.approval
    report = asyncio.run(
        preflight(config, settings, environ=environ, approval=approval, inspect_db=True)
    )
    contact, _ = _contact(config, environ)

    if args.command == "preflight":
        if args.live_robots:
            granted, note = read_approval(approval, config, "live_robots_check")
            if not granted:
                report["live_robots"] = f"REFUSED: {note}"
            elif contact is None:
                report["live_robots"] = "REFUSED: contact not configured"
            else:
                report["live_robots"] = asyncio.run(live_robots(config, contact))
        print(json.dumps(report, indent=2, sort_keys=True))
        return EXIT_BLOCKED if report["blockers"] else EXIT_OK

    # crawl
    granted, note = read_approval(approval, config, "live_crawl")
    problems = list(report["blockers"])
    if not granted:
        problems.append(f"live crawl not approved: {note}")
    if contact is None:
        problems.append("contact not configured")
    if problems:
        print(json.dumps({"refused": True, "reasons": problems}, indent=2), file=sys.stderr)
        return EXIT_BLOCKED
    if contact is None:  # unreachable: reported above; keeps the type narrow without assert
        return EXIT_BLOCKED
    result = asyncio.run(run_crawl(config, settings, contact, args.screenshots))
    print(json.dumps(result, sort_keys=True))
    return EXIT_OK if result["status"] == "succeeded" else EXIT_BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())
