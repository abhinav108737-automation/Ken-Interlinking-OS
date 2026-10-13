"""Environment doctor for the Ken Interlink OS development stack.

Prints a matrix of facts about this machine and the running stack, and exits
non-zero ONLY on hard failures. Its output is pasted verbatim into each phase
report, which is what makes a "the stack works" claim checkable.

    .venv/bin/python infrastructure/scripts/doctor.py

Statuses: OK, WARN (works, but something optional is absent), FAIL (hard
failure; exit 1), INFO (a fact, not a judgement).

It never prints a credential: the database is described via
Settings.public_settings(), which parses the URL and keeps host/port/name only.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Status = Literal["OK", "WARN", "FAIL", "INFO"]

REPO_ROOT = Path(__file__).resolve().parents[2]
NATIVE_POSTGRES_PORT = 5432  # the developer's own PostgreSQL — never the project DB
PROJECT_POSTGRES_PORT = 5433


@dataclass(frozen=True)
class Row:
    status: Status
    check: str
    detail: str


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    return proc.returncode, (proc.stdout or proc.stderr).strip()


def check_python() -> list[Row]:
    rows: list[Row] = []
    version = ".".join(map(str, sys.version_info[:3]))
    if sys.version_info[:2] == (3, 12):
        rows.append(Row("OK", "python", f"{version} ({sys.executable})"))
    else:
        rows.append(
            Row("FAIL", "python", f"{version} — Python 3.12 required; use .venv/bin/python")
        )
    in_venv = sys.prefix != sys.base_prefix
    rows.append(
        Row("OK" if in_venv else "FAIL", "virtualenv", sys.prefix if in_venv else "not in a venv")
    )
    return rows


def check_packages() -> list[Row]:
    rows: list[Row] = []
    for name in ("ken_contracts", "ken_core", "ken_api", "ken_agents"):
        try:
            module = importlib.import_module(name)
        except Exception as exc:
            rows.append(Row("FAIL", f"import {name}", f"{type(exc).__name__}: {exc}"))
            continue
        where = Path(module.__file__ or "?").resolve()
        with contextlib.suppress(ValueError):
            where = where.relative_to(REPO_ROOT)
        rows.append(Row("OK", f"import {name}", str(where.parent)))
    return rows


async def _database_facts(database_url: str) -> dict[str, str | None]:
    """Read-only facts about the configured database, over the app's own driver.

    Uses SQLAlchemy's async engine with the exact KEN_DATABASE_URL the app uses,
    so host/port/query resolution is identical to the app and to the 5432
    guard (asyncpg is async-only; there is no synchronous connection).
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(database_url, poolclass=NullPool, connect_args={"timeout": 3})
    try:
        async with engine.connect() as conn:

            async def scalar(sql: str) -> str | None:
                value = (await conn.execute(text(sql))).scalar()
                return None if value is None else str(value)

            has_alembic = await scalar("select to_regclass('public.alembic_version')")
            return {
                "server_version": await scalar("show server_version"),
                "vector_available": await scalar(
                    "select default_version from pg_available_extensions where name = 'vector'"
                ),
                "vector_installed": await scalar(
                    "select extversion from pg_extension where extname = 'vector'"
                ),
                "db_revision": (
                    await scalar("select version_num from alembic_version") if has_alembic else None
                ),
            }
    finally:
        await engine.dispose()


def check_settings_and_database() -> list[Row]:
    rows: list[Row] = []
    try:
        from ken_core.config import Settings

        settings = Settings()
    except Exception as exc:
        # hide_input_in_errors on Settings keeps credentials out of this message
        return [Row("FAIL", "settings", f"{type(exc).__name__}: {exc}")]

    public = settings.public_settings()
    host, port, name = public["database_host"], public["database_port"], public["database_name"]
    rows.append(Row("OK", "settings", f"environment={settings.environment} phase={settings.phase}"))
    rows.append(
        Row(
            "FAIL" if settings.auto_approve_enabled else "OK",
            "auto_approve",
            "DISABLED (TRD §20)"
            if not settings.auto_approve_enabled
            else "ENABLED — forbidden in v1",
        )
    )

    if settings.targets_native_postgres() and not settings.allow_native_postgres_port:
        rows.append(
            Row(
                "FAIL",
                "database target",
                f"{host}:{port}/{name} — port 5432 is the developer's OWN PostgreSQL, "
                f"not the project container ({PROJECT_POSTGRES_PORT}). Check KEN_DATABASE_URL.",
            )
        )
        # Stop here: do not open even a read-only connection to someone
        # else's server. The FAIL above is the whole answer.
        return rows
    else:
        rows.append(Row("INFO", "database target", f"{host}:{port}/{name}"))

    try:
        facts = asyncio.run(_database_facts(settings.database_url))
        rows.append(Row("OK", "postgres", f"reachable, server {facts['server_version']}"))
        available, installed = facts["vector_available"], facts["vector_installed"]
        if available is None:
            rows.append(
                Row(
                    "WARN",
                    "pgvector",
                    "NOT available on this server (native degraded mode; ADR-0003)",
                )
            )
        else:
            state = f"enabled {installed}" if installed else "not enabled (Phase 2)"
            rows.append(Row("OK", "pgvector", f"available {available} (provisioned), {state}"))
        db_revision = facts["db_revision"]
    except Exception as exc:
        rows.append(Row("FAIL", "postgres", f"unreachable: {type(exc).__name__}"))
        return rows

    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        ini = REPO_ROOT / settings.alembic_ini_path
        head = ScriptDirectory.from_config(Config(str(ini))).get_current_head()
    except Exception as exc:
        rows.append(Row("FAIL", "migrations", f"cannot read alembic scripts: {exc}"))
        return rows
    if db_revision == head:
        rows.append(Row("OK", "migrations", f"database at head ({head})"))
    else:
        rows.append(
            Row(
                "FAIL",
                "migrations",
                f"database at {db_revision!r}, code head {head!r} — run: make migrate",
            )
        )
    return rows


def check_ollama() -> list[Row]:
    rows: list[Row] = []
    try:
        import httpx

        from ken_contracts.model import ModelRole
        from ken_core.config import Settings

        settings = Settings()
        response = httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=3.0, trust_env=False)
        response.raise_for_status()
        installed = {m["name"] for m in response.json().get("models", [])}
    except Exception as exc:
        return [Row("WARN", "ollama", f"unreachable ({type(exc).__name__}) — optional in Phase 0")]

    rows.append(
        Row(
            "OK",
            "ollama",
            f"reachable at {settings.ollama_base_url}, provider={settings.model_provider}",
        )
    )
    normalised = installed | {n.removesuffix(":latest") for n in installed}
    for role in ModelRole:
        model = settings.role_model(role)
        present = model in normalised or f"{model}:latest" in installed
        if present:
            rows.append(Row("OK", f"model role {role.value}", model))
        else:
            note = (
                "not pulled — expected in Phase 0" if role is ModelRole.EMBEDDING else "not pulled"
            )
            rows.append(Row("WARN", f"model role {role.value}", f"{model} — {note}"))
    return rows


def check_toolchain() -> list[Row]:
    rows: list[Row] = []
    code, out = _run(["node", "--version"])
    if code != 0:
        rows.append(Row("FAIL", "node", "not found"))
    else:
        major, minor, patch = (int(x) for x in out.lstrip("v").split(".")[:3])
        # The real floor is set by jsdom 30 / vitest 5, stricter than next@16's
        # >=20.9. Mirrors "engines" in package.json.
        ok = (
            (major == 22 and (minor, patch) >= (22, 2))
            or (major == 24 and (minor, patch) >= (15, 0))
            or major >= 26
        )
        rows.append(
            Row("OK" if ok else "FAIL", "node", f"{out} (needs ^22.22.2 || ^24.15.0 || >=26)")
        )

    if shutil.which("docker") is None:
        rows.append(Row("WARN", "docker", "CLI not found — compose path unavailable"))
        return rows
    code, out = _run(["docker", "compose", "version", "--short"])
    rows.append(
        Row("OK" if code == 0 else "WARN", "docker compose", out if code == 0 else "plugin missing")
    )
    code, out = _run(["colima", "status"])
    rows.append(
        Row("OK" if code == 0 else "WARN", "colima", "running" if code == 0 else "not running")
    )
    code, out = _run(
        [
            "docker",
            "compose",
            "--project-directory",
            str(REPO_ROOT),
            "ps",
            "--format",
            "{{.Service}}={{.Status}}",
        ]
    )
    services = out.replace("\n", ", ") if code == 0 and out else "none running"
    rows.append(Row("INFO", "compose services", services))
    return rows


def main() -> int:
    sections = [
        ("Interpreter", check_python),
        ("Packages", check_packages),
        ("Configuration & database", check_settings_and_database),
        ("Model server", check_ollama),
        ("Toolchain & containers", check_toolchain),
    ]
    all_rows: list[Row] = []
    print("Ken Interlink OS — doctor\n")
    for title, fn in sections:
        rows = fn()
        all_rows.extend(rows)
        print(f"== {title}")
        for row in rows:
            print(f"  [{row.status:<4}] {row.check:<20} {row.detail}")
        print()
    failures = [r for r in all_rows if r.status == "FAIL"]
    warnings = [r for r in all_rows if r.status == "WARN"]
    print(f"Summary: {len(failures)} FAIL, {len(warnings)} WARN, {len(all_rows)} checks")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
