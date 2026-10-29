"""Lifecycle subcommands of ``python -m ken_services.learning.cli`` (workstream D).

::

    shadow            --candidate UUID [--run UUID]
    policy-promote    --proposal UUID [--apply]
    experiment-create --key KEY [--kind policy_ab|holdout] [--candidate UUID] [--fraction F]
                      [--description TEXT] --as-user NAME
    experiment-assign --key KEY
    experiment-activate --key KEY --as-user NAME
    experiment-end      --key KEY --as-user NAME

``cli.py`` dispatches these here BEFORE its own parser runs, the way it dispatches ``eval``.

CLI-ONLY, deliberately: none of this is reachable over HTTP. ``--as-user`` names an enabled
account holding the ``promote`` permission, looked up in the database (the autonomy CLI's
pattern); the CLI cannot invent an operator. Nothing here promotes a proposal, and
``policy-promote`` REFUSES unless a human ``promoted`` transition citing an evaluation already
exists -- it cannot create one, and with ``learning_demonstration_mode`` at its default no such
transition can exist at all.

Prints ONE JSON line. Exit codes: 0 done; 1 refused or failed; 2 usage or configuration error.
Output never contains a database URL, a file system path outside the repo, or exception text.
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
from sqlalchemy import text

from ken_contracts.auth import Permission, Principal, has_permission
from ken_contracts.learning import (
    LEARNING_ACTOR,
    ExperimentKind,
    ExperimentRefusedError,
    LearningError,
    PromotionRefusedError,
    ProposalStatus,
)
from ken_core import logging as ken_logging
from ken_core.config import ConfigurationError, Settings, get_settings
from ken_core.db.engine import create_engine
from ken_core.db.repositories import auth as auth_repo
from ken_core.db.repositories import learning as repo
from ken_core.db.session import create_sessionmaker
from ken_services.learning.experiments import ExperimentLifecycleService
from ken_services.learning.lifecycle import advance_system_stage
from ken_services.learning.policy import PolicyError, load_configured_learning_policy
from ken_services.learning.promote import PolicyWriteRefusedError, write_promoted_policy
from ken_services.learning.shadow import ShadowRefusal, ShadowRefusedError, shadow_pass

__all__ = ["COMMANDS", "main"]

EXIT_OK: Final = 0
EXIT_RUN_ERROR: Final = 1
EXIT_USAGE: Final = 2
COMMANDS: Final = frozenset(
    {
        "shadow",
        "policy-promote",
        "experiment-create",
        "experiment-assign",
        "experiment-activate",
        "experiment-end",
    }
)


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a UUID") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ken_services.learning.cli",
        description="Learning lifecycle: shadow, policy file promotion, experiments (ADR-0016).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    shadow = commands.add_parser(
        "shadow", help="Re-run selection with a candidate beside production; writes no plans."
    )
    shadow.add_argument("--candidate", required=True, type=_uuid)
    shadow.add_argument("--run", default=None, type=_uuid, help="default: latest recommend run")
    promote = commands.add_parser(
        "policy-promote",
        help="Write the promoted policy file(s). Refuses without a human promoted transition.",
    )
    promote.add_argument("--proposal", required=True, type=_uuid)
    promote.add_argument("--apply", action="store_true", help="write (default: dry run)")
    create = commands.add_parser("experiment-create", help="Create a DRAFT experiment.")
    create.add_argument("--key", required=True)
    create.add_argument(
        "--kind", choices=[kind.value for kind in ExperimentKind], default="policy_ab"
    )
    create.add_argument("--candidate", default=None, type=_uuid)
    create.add_argument("--fraction", default=None, type=float)
    create.add_argument("--description", default="experiment created from the CLI")
    create.add_argument("--as-user", dest="as_user", default=None)
    assign = commands.add_parser("experiment-assign", help="Materialise arms (draft only).")
    assign.add_argument("--key", required=True)
    for name in ("experiment-activate", "experiment-end"):
        sub = commands.add_parser(name, help=f"{name.replace('-', ' ')} (needs --as-user).")
        sub.add_argument("--key", required=True)
        sub.add_argument("--as-user", dest="as_user", default=None)
    return parser


async def _principal(settings: Settings, username: str | None) -> Principal | str:
    """An enabled account holding ``promote``, or a fixed refusal string (never the name)."""
    if not username:
        return "configuration error: this command needs --as-user USERNAME"
    engine = create_engine(settings)
    try:
        sessionmaker = create_sessionmaker(engine)
        async with sessionmaker() as session:
            record = await auth_repo.get_user_by_username(session, username=username)
            await session.rollback()
    finally:
        await engine.dispose()
    if record is None:
        return "configuration error: --as-user names no known account"
    if record.disabled or not has_permission(record.role, Permission.PROMOTE):
        return "configuration error: --as-user needs an enabled account with promote permission"
    return Principal(
        user_id=record.id,
        username=record.username,
        display_name=record.display_name,
        role=record.role,
        session_id=uuid.uuid4(),
    )


async def _latest_recommendation_run(session: Any) -> uuid.UUID | None:
    found = await session.scalar(
        text(
            "SELECT id FROM job_runs WHERE kind = 'recommendation' "
            "AND status IN ('succeeded', 'completed_with_errors') "
            "ORDER BY started_at DESC LIMIT 1"
        )
    )
    return found if isinstance(found, uuid.UUID) else None


async def _shadow(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    policy = load_configured_learning_policy(settings)
    engine = create_engine(settings)
    try:
        sessionmaker = create_sessionmaker(engine)
        async with sessionmaker() as session:
            candidate = await repo.candidate_by_id(session, args.candidate)
            proposal = (
                None
                if candidate is None
                else await repo.proposal_by_id(session, candidate.proposal_id)
            )
            run_id = args.run or await _latest_recommendation_run(session)
            await session.rollback()
        if candidate is None or proposal is None:
            raise ShadowRefusedError(ShadowRefusal.CANDIDATE_NOT_FOUND)
        if run_id is None:
            raise ShadowRefusedError(ShadowRefusal.NO_RECOMMENDATION_RUN)
        if proposal.status == ProposalStatus.OFFLINE_TEST.value:
            await advance_system_stage(
                sessionmaker, proposal_id=proposal.id, to_status=ProposalStatus.SHADOW
            )
        async with sessionmaker() as session, session.begin():
            evaluation_id = await shadow_pass(
                session,
                settings=settings,
                policy=policy,
                candidate_id=args.candidate,
                recommendation_run_id=run_id,
            )
            row = await repo.evaluation_by_id(session, evaluation_id)
            diff = dict((row.metrics if row is not None else {}).get("shadow") or {})
        return {
            "command": "shadow",
            "evaluation_id": str(evaluation_id),
            "recommendation_run_id": str(run_id),
            "added": diff.get("added_count"),
            "dropped": diff.get("dropped_count"),
            "reordered": diff.get("reordered_count"),
            "compared_pairs": diff.get("compared_pairs"),
            "plans_written": 0,
        }
    finally:
        await engine.dispose()


async def _promote(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    engine = create_engine(settings)
    try:
        paths = await write_promoted_policy(
            sessionmaker=create_sessionmaker(engine),
            settings=settings,
            proposal_id=args.proposal,
            dry_run=not args.apply,
        )
    finally:
        await engine.dispose()
    return {
        "command": "policy-promote",
        "dry_run": not args.apply,
        "paths": list(paths),
        "requires_operator_restart": True,
    }


async def _experiment(settings: Settings, args: argparse.Namespace) -> dict[str, Any] | str:
    command = args.command
    principal: Principal | None = None
    if command in ("experiment-create", "experiment-activate", "experiment-end"):
        found = await _principal(settings, args.as_user)
        if isinstance(found, str):
            return found
        principal = found
    engine = create_engine(settings)
    try:
        service = ExperimentLifecycleService(
            sessionmaker=create_sessionmaker(engine),
            settings=settings,
            policy=load_configured_learning_policy(settings),
        )
        if command == "experiment-create" and principal is not None:
            kind = ExperimentKind(args.kind)
            experiment_id = await _create(service, principal, args, kind)
            return {"command": command, "experiment_id": str(experiment_id), "status": "draft"}
        if command == "experiment-assign":
            counts = await service.assign(key=args.key, actor=LEARNING_ACTOR)
            return {
                "command": command,
                "arms": {arm.value: n for arm, n in sorted(counts.items(), key=lambda i: i[0])},
            }
        if principal is None:  # pragma: no cover - guarded above
            return "configuration error: this command needs --as-user USERNAME"
        if command == "experiment-activate":
            await service.activate(principal=principal, key=args.key)
            return {"command": command, "status": "active"}
        await service.end(principal=principal, key=args.key)
        return {"command": command, "status": "ended"}
    finally:
        await engine.dispose()


async def _create(
    service: ExperimentLifecycleService,
    principal: Principal,
    args: argparse.Namespace,
    kind: ExperimentKind,
) -> uuid.UUID:
    return await service.create(
        principal=principal,
        key=args.key,
        description=args.description,
        kind=kind,
        candidate_id=args.candidate,
        treatment_fraction=args.fraction,
    )


async def _run(settings: Settings, args: argparse.Namespace) -> int:
    try:
        match args.command:
            case "shadow":
                line: dict[str, Any] | str = await _shadow(settings, args)
            case "policy-promote":
                line = await _promote(settings, args)
            case _:
                line = await _experiment(settings, args)
    except (PolicyError, FileNotFoundError, OSError):
        print("configuration error: a policy file could not be loaded", file=sys.stderr)
        return EXIT_USAGE
    except (ShadowRefusedError, PolicyWriteRefusedError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_RUN_ERROR
    except (PromotionRefusedError, ExperimentRefusedError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_RUN_ERROR
    except LearningError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_RUN_ERROR
    except Exception as exc:
        print(f"the command could not be completed ({type(exc).__name__})", file=sys.stderr)
        return EXIT_RUN_ERROR
    if isinstance(line, str):
        print(line, file=sys.stderr)
        return EXIT_USAGE
    print(json.dumps(line, sort_keys=True))
    return EXIT_OK


def main(argv: Sequence[str]) -> int:
    try:
        args = _parser().parse_args(list(argv))
    except SystemExit as exc:
        return EXIT_USAGE if exc.code not in (0, None) else EXIT_OK
    try:
        settings = get_settings()
    except (ValidationError, ConfigurationError):
        print("configuration error: invalid KEN_* settings", file=sys.stderr)
        return EXIT_USAGE
    ken_logging.configure_logging(level=settings.log_level, fmt=settings.log_format)
    return asyncio.run(_run(settings, args))
