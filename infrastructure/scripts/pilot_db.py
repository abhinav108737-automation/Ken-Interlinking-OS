"""Create the LOCAL, separate database for a public-crawl pilot (never the synthetic ones).

    .venv/bin/python infrastructure/scripts/pilot_db.py --config pilots/kenresearch/pilot-25.yaml

Runs as the OWNER on the project container (port 5433 only; 5432 is refused): creates the
pilot's database if absent, migrates it to head and applies the ``ken_app`` grants with the
application password already in ``KEN_DATABASE_URL``. Sends nothing to any website. Idempotent.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "infrastructure" / "scripts")]

import db_roles  # noqa: E402

from ken_core.config import Settings  # noqa: E402
from ken_services.inventory.pilot import load_pilot  # noqa: E402


async def _create(owner_admin_url: URL, database: str) -> bool:
    engine = create_async_engine(
        owner_admin_url,
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
    )
    try:
        async with engine.connect() as connection:
            exists = await connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": database}
            )
            if exists:
                return False
            await connection.execute(text(f'CREATE DATABASE "{database}"'))
            return True
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = load_pilot(args.config if args.config.is_absolute() else REPO / args.config)
    settings = Settings()
    owner = make_url(settings.effective_migration_database_url)
    app = make_url(settings.database_url)
    database = config.destination.database
    target_owner = owner.set(database=database)
    db_roles.assert_safe_target(target_owner)
    created = asyncio.run(_create(owner.set(database="postgres"), database))
    env = dict(os.environ)
    env["KEN_MIGRATION_DATABASE_URL"] = target_owner.render_as_string(hide_password=False)
    subprocess.run(
        [str(REPO / ".venv" / "bin" / "alembic"), "upgrade", "head"], check=True, cwd=REPO, env=env
    )
    password = app.password
    asyncio.run(db_roles.apply_role(target_owner, password=password, rotate=False))
    print(f"{'created' if created else 'reused'} database {database}; migrated; ken_app granted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
