"""Run the whole OFFLINE interlinking demo in shadow mode and write the management report.

    .venv/bin/python evals/offline_demo/run_demo.py [--fresh] [--rule-critic]
        [--html-dir FOLDER --html-site-url URL --confirm-authorized]

Everything is local: PostgreSQL on port 5433 (database ``ken_offline_demo``), Qdrant on
127.0.0.1 (collection ``ken_offline_demo_pages``) and Ollama on 127.0.0.1. Nothing contacts a
website, the CMS is the in-memory mock, nothing is published and ``KEN_AUTONOMY_MODE`` is forced
to ``shadow``.

Content: the 65-page synthetic corpus (``synthetic_base``) plus the 11-page synthetic extension
(``synthetic_extension``). An operator may add a folder of HTML files they are ALREADY authorised to
use with ``--html-dir`` (labelled ``authorized_import``); the flag ``--confirm-authorized`` is the
operator's statement that they are, and without it the folder is refused.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "services"), str(ROOT / "contracts" / "python")]

DATABASE = "ken_offline_demo"
COLLECTION = "ken_offline_demo_pages"
BASE = ROOT / "tests" / "fixtures" / "demo" / "research_site.jsonl"
EXTENSION = ROOT / "tests" / "fixtures" / "offline_demo" / "synthetic_extension.jsonl"
RULE_AGENTS_POLICY = ROOT / "evals" / "offline_demo" / "agents.rule.yaml"
PY = sys.executable


def environment(rule_critic: bool) -> dict[str, str]:
    from ken_core.config import Settings

    settings = Settings()
    app = make_url(settings.database_url).set(database=DATABASE)
    env = dict(os.environ)
    env.update(
        {
            "KEN_DATABASE_URL": app.render_as_string(hide_password=False),
            "KEN_QDRANT_COLLECTION": COLLECTION,
            "KEN_VECTOR_STORE": "qdrant",
            "KEN_AUTONOMY_MODE": "shadow",
            "KEN_AUTONOMY_AUTONOMOUS_ACKNOWLEDGED": "false",
            "KEN_AUTONOMY_ALLOW_PRODUCTION": "false",
            "KEN_CMS_PROVIDER": "mock",
        }
    )
    if rule_critic:
        env["KEN_AGENTS_POLICY_PATH"] = str(RULE_AGENTS_POLICY)
    return env


def step(title: str, argv: list[str], env: dict[str, str]) -> str:
    print(f"== {title}", flush=True)
    awake = ["caffeinate", "-dims"] if shutil.which("caffeinate") else []
    done = subprocess.run(  # noqa: S603
        [*awake, *argv], cwd=ROOT, env=env, capture_output=True, text=True, check=False
    )
    lines = [ln for ln in done.stdout.splitlines() if ln.strip().startswith("{")]
    last = lines[-1] if lines else ""
    if done.returncode not in (0, 1):  # 1 = completed_with_errors is reported, not fatal
        print(done.stderr[-800:], file=sys.stderr)
        raise SystemExit(f"step failed: {title} (exit {done.returncode})")
    print(f"   {last[:400]}", flush=True)
    return last


def qdrant_delete(collection: str) -> None:
    request = urllib.request.Request(
        f"http://127.0.0.1:6333/collections/{collection}", method="DELETE"
    )
    with contextlib.suppress(urllib.error.URLError):
        urllib.request.urlopen(request, timeout=10)  # noqa: S310


def drop_database() -> None:
    """Owner connection, this one database name only."""
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from ken_core.config import Settings

    owner = make_url(Settings().effective_migration_database_url).set(database="postgres")
    if owner.port == 5432:
        raise SystemExit("refusing port 5432")

    async def go() -> None:
        engine = create_async_engine(owner, poolclass=NullPool, isolation_level="AUTOCOMMIT")
        async with engine.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{DATABASE}" WITH (FORCE)'))
        await engine.dispose()

    asyncio.run(go())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fresh", action="store_true", help="drop and recreate the demo database")
    parser.add_argument("--rule-critic", action="store_true", help="deterministic critic (fast)")
    parser.add_argument("--html-dir", type=Path, default=None)
    parser.add_argument("--html-site-url", default=None)
    parser.add_argument("--confirm-authorized", action="store_true")
    parser.add_argument("--skip-report", action="store_true")
    args = parser.parse_args()
    if args.html_dir and not (args.confirm_authorized and args.html_site_url):
        raise SystemExit(
            "--html-dir needs --html-site-url and --confirm-authorized "
            "(your statement that you may already use these files)"
        )
    env = environment(args.rule_critic)
    if args.fresh:
        qdrant_delete(COLLECTION)
        drop_database()
    step(
        "create/migrate the demo database",
        [PY, "infrastructure/scripts/pilot_db.py", "--database", DATABASE],
        env,
    )
    inv = [PY, "-m", "ken_services.inventory.cli"]
    step(
        "import synthetic base corpus",
        [*inv, "import-file", str(BASE), "--provenance", "synthetic_base"],
        env,
    )
    step(
        "import synthetic extension",
        [*inv, "import-file", str(EXTENSION), "--provenance", "synthetic_extension"],
        env,
    )
    if args.html_dir:
        step(
            "import authorised HTML folder",
            [
                *inv,
                "import-html-dir",
                str(args.html_dir),
                "--site-url",
                args.html_site_url,
                "--provenance",
                "authorized_import",
            ],
            env,
        )
    step(
        "analyse + embed (Ollama, local)",
        [PY, "-m", "ken_services.intelligence.cli", "analyze"],
        env,
    )
    step("index into Qdrant", [PY, "-m", "ken_services.vectors.cli", "index"], env)
    step("verify the index", [PY, "-m", "ken_services.vectors.cli", "verify"], env)
    step("link-graph metrics", [PY, "-m", "ken_services.graph.cli", "compute"], env)
    step(
        "candidates, ranking, anchors",
        [PY, "-m", "ken_services.recommendations.cli", "recommend"],
        env,
    )
    step("critic + policy gate", [PY, "-m", "ken_agents.cli", "agents"], env)
    step(
        "shadow autonomy decisions",
        [PY, "-m", "ken_services.autonomy.cli", "evaluate", "--mode", "shadow"],
        env,
    )
    if not args.skip_report:
        step("management report", [PY, "evals/offline_demo/report.py"], env)
    print("done: nothing was published; mode shadow; CMS mock")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
