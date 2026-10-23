"""Autonomy command line: ``python -m ken_services.autonomy.cli`` (ADR-0018 s5).

CLI-ONLY, like every other job in this system: no HTTP route and no UI button starts an
evaluation pass or a publish sweep. The API has GET routes plus ONE operator-override write
route; this is where passes are run.

    evaluate [--mode shadow|guarded|autonomous] [--source PAGE_ID ...] [--plan PLAN_ID ...]
             [--as-user USERNAME] [--dry-run]

What a pass does depends on ``KEN_AUTONOMY_MODE``, whose default is ``shadow``:

* ``shadow``     evaluate every decidable plan and record the counterfactual. Publishes
                 nothing; needs no operator identity and no CMS provider.
* ``guarded``    publish low-risk, high-confidence, CALIBRATED decisions; block the rest.
* ``autonomous`` publish every policy-eligible decision. Hard blocks still apply.

``--mode`` may only make a run MORE conservative. Asking for ``autonomous`` on a ``shadow``
installation is a usage error, not a privilege escalation: moving off ``shadow`` takes
``KEN_AUTONOMY_MODE`` plus ``KEN_AUTONOMY_AUTONOMOUS_ACKNOWLEDGED`` (and
``KEN_AUTONOMY_ALLOW_PRODUCTION`` for a live site), which ``Settings`` checks at startup.

``--as-user`` names the operator account a publishing pass acts as. It is REQUIRED outside
shadow mode, because ``deployments.actor`` carries a ``^user:`` CHECK and ``user_id`` is a
NOT NULL foreign key to ``users``: a CMS write has to be attributable to a real, enabled
account with the ``deploy`` permission. It is not an approval -- nobody is asked about any
individual link -- it is the accountable identity for the audit trail, chosen once per run.
Create it with ``make user-create ROLE=operator_admin``.

``--dry-run`` evaluates and prints, recording nothing at all: no decision rows, no
transitions, no attempts. Useful for seeing what a mode change would do before making it.

Prints one JSON line (the mode, the job kind, the run status and the counters). Exit codes:

    0  the pass completed with no errors
    1  the pass completed with errors, or could not run
    2  usage or configuration error (nothing was run)

Never prints a database or CMS URL, a token or exception text: errors name a class only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Sequence
from typing import Any, Final

from pydantic import ValidationError

from ken_contracts.auth import Permission, Principal, has_permission
from ken_contracts.autonomy import AutonomyMode
from ken_contracts.inventory import RunStatus
from ken_core import logging as ken_logging
from ken_core.config import ConfigurationError, Settings, get_settings
from ken_core.db.engine import create_engine
from ken_core.db.repositories import auth as auth_repo
from ken_core.db.session import create_sessionmaker
from ken_core.providers.cms.factory import build_cms_provider
from ken_services.autonomy.compose import build_engine
from ken_services.autonomy.engine import CriticDeclinedError
from ken_services.autonomy.gate import PublishingNotEnabledError
from ken_services.autonomy.job import job_kind_for, run_autonomy
from ken_services.jobs.runner import JobAlreadyRunningError

__all__ = [
    "EXIT_OK",
    "EXIT_RUN_ERRORS",
    "EXIT_USAGE",
    "NO_USER_REFUSAL",
    "main",
]

EXIT_OK: Final = 0
EXIT_RUN_ERRORS: Final = 1
EXIT_USAGE: Final = 2

NO_USER_REFUSAL: Final = (
    "configuration error: a publishing autonomy mode needs --as-user USERNAME (an enabled "
    "account with the deploy permission). A CMS write must be attributable to a real "
    "operator account; shadow mode needs none because it writes nothing"
)
ROLE_REFUSAL: Final = (
    "configuration error: --as-user names an account without the deploy permission, or a "
    "disabled one"
)
UNKNOWN_USER_REFUSAL: Final = "configuration error: --as-user names no known account"


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a UUID") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ken_services.autonomy.cli",
        description="Run an autonomy evaluation (and, outside shadow mode, publish) pass.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("evaluate", help="evaluate eligible plans under KEN_AUTONOMY_MODE")
    run.add_argument(
        "--mode",
        choices=[mode.value for mode in AutonomyMode],
        default=None,
        help="force a MORE conservative mode than KEN_AUTONOMY_MODE (never a laxer one)",
    )
    run.add_argument(
        "--source", action="append", type=_uuid, default=None, metavar="PAGE_ID", dest="sources"
    )
    run.add_argument(
        "--plan", action="append", type=_uuid, default=None, metavar="PLAN_ID", dest="plans"
    )
    run.add_argument("--as-user", default=None, metavar="USERNAME", dest="as_user")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="evaluate and print; record nothing (no decisions, no transitions, no attempts)",
    )
    return parser


async def _principal(settings: Settings, username: str) -> Principal | str:
    """The operator identity, or a refusal string. Never echoes the username in a refusal."""
    engine = create_engine(settings)
    try:
        sessionmaker = create_sessionmaker(engine)
        async with sessionmaker() as session:
            record = await auth_repo.get_user_by_username(session, username=username)
            await session.rollback()
    finally:
        await engine.dispose()
    if record is None:
        return UNKNOWN_USER_REFUSAL
    if record.disabled or not has_permission(record.role, Permission.DEPLOY):
        return ROLE_REFUSAL
    return Principal(
        user_id=record.id,
        username=record.username,
        display_name=record.display_name,
        role=record.role,
        # The CLI has no HTTP session. The field is a correlation id in every consumer that
        # reads it, and the deployment service does not read it at all, so a fresh value is
        # honest: this pass is its own "session".
        session_id=uuid.uuid4(),
    )


async def _dry_run(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    """Evaluate without recording anything and return the decision counts."""
    engine = create_engine(settings)
    try:
        sessionmaker = create_sessionmaker(engine)
        autonomy = build_engine(sessionmaker=sessionmaker, settings=settings)
        mode = autonomy.resolve_mode(None if args.mode is None else AutonomyMode(args.mode))
        ids = (
            list(args.plans)
            if args.plans
            else await autonomy.decidable_plans(source_page_ids=args.sources)
        )
        counts: dict[str, int] = {}
        for plan_id in ids:
            try:
                evaluation = await autonomy.evaluate(plan_id=plan_id, mode=mode)
            except CriticDeclinedError:
                counts["declined"] = counts.get("declined", 0) + 1
                continue
            key = (
                evaluation.would_have_decision.value
                if evaluation.would_have_decision is not None
                else evaluation.decision.value
            )
            counts[key] = counts.get(key, 0) + 1
        return {
            "command": "autonomy-evaluate",
            "dry_run": True,
            "mode": mode.value,
            "plans": len(ids),
            "decisions": dict(sorted(counts.items())),
        }
    finally:
        await engine.dispose()


async def _run(settings: Settings, args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    engine = create_engine(settings)
    cms = None
    try:
        sessionmaker = create_sessionmaker(engine)
        autonomy = build_engine(sessionmaker=sessionmaker, settings=settings)
        mode = autonomy.resolve_mode(None if args.mode is None else AutonomyMode(args.mode))
        principal: Principal | None = None
        if mode is not AutonomyMode.SHADOW:
            if not args.as_user:
                print(NO_USER_REFUSAL, file=sys.stderr)
                return EXIT_USAGE, {}
            found = await _principal(settings, args.as_user)
            if isinstance(found, str):
                print(found, file=sys.stderr)
                return EXIT_USAGE, {}
            principal = found
            cms = build_cms_provider(settings)
        detail = await run_autonomy(
            sessionmaker=sessionmaker,
            settings=settings,
            triggered_by="cli",
            mode=mode,
            plan_ids=list(args.plans) if args.plans else None,
            source_page_ids=list(args.sources) if args.sources else None,
            principal=principal,
            cms=cms,
            engine=autonomy,
        )
    finally:
        closer = getattr(cms, "close", None)
        if closer is not None:
            await closer()
        await engine.dispose()
    code = EXIT_OK if RunStatus(detail.status) is RunStatus.SUCCEEDED else EXIT_RUN_ERRORS
    return code, {
        "command": "autonomy-evaluate",
        "mode": mode.value,
        "kind": job_kind_for(mode).value,
        "run_id": str(detail.id),
        "status": RunStatus(detail.status).value,
        "counters": dict(sorted(detail.counters.items())),
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    ken_logging.configure_logging()
    try:
        settings = get_settings()
    except (ConfigurationError, ValidationError) as exc:
        # A refused autonomy configuration (ADR-0018 s2) lands here: the message names the
        # setting, never its value.
        print(f"configuration error ({type(exc).__name__})", file=sys.stderr)
        return EXIT_USAGE
    try:
        if args.dry_run:
            payload = asyncio.run(_dry_run(settings, args))
            print(json.dumps(payload, sort_keys=True))
            return EXIT_OK
        code, payload = asyncio.run(_run(settings, args))
    except ValueError as exc:
        print(f"usage error ({type(exc).__name__})", file=sys.stderr)
        return EXIT_USAGE
    except PublishingNotEnabledError as exc:
        # The refusal CODE only, never a URL or a setting's value.
        print(f"publishing is not enabled ({exc.refusal.value})", file=sys.stderr)
        return EXIT_USAGE
    except JobAlreadyRunningError:
        print("another autonomy pass of this kind is in progress", file=sys.stderr)
        return EXIT_RUN_ERRORS
    except Exception as exc:  # never exception text: it can carry a URL or a credential
        print(f"the autonomy pass could not complete ({type(exc).__name__})", file=sys.stderr)
        return EXIT_RUN_ERRORS
    if payload:
        print(json.dumps(payload, sort_keys=True))
    return code


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
