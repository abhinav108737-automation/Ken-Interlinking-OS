"""Bulk deployment operator commands: ``python -m ken_services.deployment.batch_cli``.

The one-off scripts used for the 2026-11 Ken Consulting live test bypassed the engine and the
review gate. This is the controlled equivalent: it drives the SAME
:class:`DatabaseDeploymentService` the API uses, so every link still passes the per-source lock,
the stored-intent / idempotency machinery, dry-run validation and post-deploy verification.

    status
    apply    --as-user U [--source ID ...] [--max-sources N] [--execute] [--out FILE]
    rollback --as-user U (--deployment ID ... | --all-live) --reason R [--note T] [--execute]

Safety properties, each enforced in code and tested:

* **Dry-run is the default.** ``apply`` / ``rollback`` validate against the CMS (reads only) and
  change nothing unless ``--execute`` is given.
* **``--execute`` needs ``KEN_CMS_WRITES_ENABLED=true``**; otherwise exit 2, nothing sent.
* **Hard batch cap.** ``--max-sources`` defaults to 5 and can never exceed 100.
* **Circuit breaker.** ``--stop-after`` consecutive failures abort the run (default 3).
* **Re-runnable.** Operation ids are deterministic (uuid5 of the plan / deployment ids), so a
  re-run replays the stored outcome instead of deploying twice.
* **Only APPROVED plans deploy.** Approval is the human review gate; nothing here bypasses it.
* **Rollback retries transient CMS faults** (the Ken Consulting rollback holds a row lock and
  answers HTTP 500 for minutes) with backoff, using the same operation id so a retry is a replay.

Output is one JSON line per source / deployment plus a final summary. It never prints a token,
a URL or exception text: failures name a class only. Exit codes: 0 all fine, 1 some item failed
or the breaker tripped, 2 usage / configuration error (nothing was sent).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.auth import Permission, Principal, has_permission
from ken_contracts.deployment import (
    MAX_PLANS_PER_DEPLOYMENT,
    DeploymentStatus,
    DeployRequest,
    DryRunRequest,
    RollbackReason,
    RollbackRequest,
)
from ken_core import logging as ken_logging
from ken_core.config import ConfigurationError, Settings, get_settings
from ken_core.db.engine import create_engine
from ken_core.db.models.deployment import Deployment
from ken_core.db.models.recommendations import LinkPlan
from ken_core.db.repositories import auth as auth_repo
from ken_core.db.session import create_sessionmaker
from ken_core.providers.cms.factory import build_cms_provider
from ken_services.deployment.compose import build_deployment_service
from ken_services.deployment.service import ROLLBACKABLE, DatabaseDeploymentService

__all__ = [
    "DEFAULT_MAX_SOURCES",
    "EXIT_OK",
    "EXIT_RUN_ERRORS",
    "EXIT_USAGE",
    "HARD_MAX_SOURCES",
    "apply_operation_id",
    "chunk",
    "main",
    "rollback_operation_id",
]

EXIT_OK: Final = 0
EXIT_RUN_ERRORS: Final = 1
EXIT_USAGE: Final = 2

DEFAULT_MAX_SOURCES: Final = 5
HARD_MAX_SOURCES: Final = 100
DEFAULT_STOP_AFTER: Final = 3
ROLLBACK_ATTEMPTS: Final = 5
ROLLBACK_BACKOFF_S: Final = (5.0, 15.0, 30.0, 60.0)
APPROVED: Final = "approved"

#: Fixed namespace: deterministic operation ids make every re-run an idempotent replay.
_NAMESPACE: Final = uuid.UUID("6b1f4a52-0c1e-4f0e-9c55-5f0d2f6a9b11")

WRITES_REFUSAL: Final = (
    "configuration error: --execute needs KEN_CMS_WRITES_ENABLED=true (nothing was sent)"
)


def apply_operation_id(plan_ids: Sequence[uuid.UUID]) -> uuid.UUID:
    """Same plans -> same operation id, whatever order they were listed in."""
    return uuid.uuid5(_NAMESPACE, "apply:" + ",".join(sorted(str(p) for p in plan_ids)))


def rollback_operation_id(deployment_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, f"rollback:{deployment_id}")


def chunk(
    items: Sequence[uuid.UUID], size: int = MAX_PLANS_PER_DEPLOYMENT
) -> list[list[uuid.UUID]]:
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a UUID") from None


def _max_sources(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a whole number") from None
    if not 1 <= number <= HARD_MAX_SOURCES:
        raise argparse.ArgumentTypeError(f"must be between 1 and {HARD_MAX_SOURCES}")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ken_services.deployment.batch_cli",
        description="Bulk, review-gated, reversible deployment (dry-run by default).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="plan and deployment counts; reads the database only")

    apply = sub.add_parser("apply", help="deploy APPROVED plans, one source page at a time")
    apply.add_argument("--as-user", required=True, dest="as_user")
    apply.add_argument("--source", action="append", type=_uuid, default=None, dest="sources")
    apply.add_argument("--max-sources", type=_max_sources, default=DEFAULT_MAX_SOURCES)
    apply.add_argument("--stop-after", type=int, default=DEFAULT_STOP_AFTER, dest="stop_after")
    apply.add_argument("--pause-s", type=float, default=1.0, dest="pause_s")
    apply.add_argument("--execute", action="store_true")
    apply.add_argument("--out", type=Path, default=None)

    rollback = sub.add_parser("rollback", help="reverse live deployments")
    rollback.add_argument("--as-user", required=True, dest="as_user")
    target = rollback.add_mutually_exclusive_group(required=True)
    target.add_argument("--deployment", action="append", type=_uuid, default=None, dest="ids")
    target.add_argument("--all-live", action="store_true", dest="all_live")
    rollback.add_argument(
        "--reason", required=True, choices=[reason.value for reason in RollbackReason]
    )
    rollback.add_argument("--note", default="")
    rollback.add_argument("--max-deployments", type=_max_sources, default=HARD_MAX_SOURCES)
    rollback.add_argument("--stop-after", type=int, default=DEFAULT_STOP_AFTER, dest="stop_after")
    rollback.add_argument("--execute", action="store_true")
    rollback.add_argument("--out", type=Path, default=None)
    return parser


@dataclass
class Tally:
    ok: int = 0
    failed: int = 0
    skipped: int = 0
    consecutive_failures: int = 0
    tripped: bool = False
    lines: list[dict[str, Any]] = field(default_factory=list)

    def record(self, line: dict[str, Any], *, failed: bool, stop_after: int) -> None:
        self.lines.append(line)
        print(json.dumps(line, sort_keys=True), flush=True)
        if failed:
            self.failed += 1
            self.consecutive_failures += 1
            if self.consecutive_failures >= stop_after:
                self.tripped = True
        else:
            self.ok += 1
            self.consecutive_failures = 0


async def _principal(
    sessionmaker: async_sessionmaker[AsyncSession], username: str
) -> Principal | None:
    async with sessionmaker() as session:
        record = await auth_repo.get_user_by_username(session, username=username)
        await session.rollback()
    if record is None or record.disabled or not has_permission(record.role, Permission.DEPLOY):
        return None
    return Principal(
        user_id=record.id,
        username=record.username,
        display_name=record.display_name,
        role=record.role,
        session_id=uuid.uuid4(),  # a CLI run is its own session
    )


async def approved_by_source(
    sessionmaker: async_sessionmaker[AsyncSession], sources: Sequence[uuid.UUID] | None
) -> dict[uuid.UUID, list[uuid.UUID]]:
    query = select(LinkPlan.source_page_id, LinkPlan.id).where(LinkPlan.status == APPROVED)
    if sources:
        query = query.where(LinkPlan.source_page_id.in_(list(sources)))
    async with sessionmaker() as session:
        rows = (await session.execute(query.order_by(LinkPlan.source_page_id, LinkPlan.id))).all()
        await session.rollback()
    grouped: dict[uuid.UUID, list[uuid.UUID]] = {}
    for source_id, plan_id in rows:
        grouped.setdefault(source_id, []).append(plan_id)
    return grouped


async def live_deployments(
    sessionmaker: async_sessionmaker[AsyncSession], ids: Sequence[uuid.UUID] | None
) -> list[uuid.UUID]:
    live = [status.value for status in ROLLBACKABLE]
    query = select(Deployment.id).where(Deployment.status.in_(live))
    if ids:
        query = query.where(Deployment.id.in_(list(ids)))
    async with sessionmaker() as session:
        rows = (await session.execute(query.order_by(Deployment.created_at))).scalars().all()
        await session.rollback()
    return list(rows)


async def _status(sessionmaker: async_sessionmaker[AsyncSession]) -> int:
    async with sessionmaker() as session:
        plans = (
            await session.execute(select(LinkPlan.status, func.count()).group_by(LinkPlan.status))
        ).all()
        deployments = (
            await session.execute(
                select(Deployment.status, func.count()).group_by(Deployment.status)
            )
        ).all()
        await session.rollback()
    print(
        json.dumps(
            {
                "command": "status",
                "plans": dict(plans),
                "deployments": dict(deployments),
            },
            sort_keys=True,
        )
    )
    return EXIT_OK


async def _apply(
    service: DatabaseDeploymentService,
    sessionmaker: async_sessionmaker[AsyncSession],
    principal: Principal,
    args: argparse.Namespace,
) -> int:
    grouped = await approved_by_source(sessionmaker, args.sources)
    selected = list(grouped.items())[: args.max_sources]
    tally = Tally()
    for source_id, plan_ids in selected:
        if tally.tripped:
            tally.skipped += 1
            continue
        for ids in chunk(plan_ids):
            operation_id = apply_operation_id(ids)
            line: dict[str, Any] = {
                "command": "apply",
                "source_page_id": str(source_id),
                "plans": len(ids),
                "executed": args.execute,
                "operation_id": str(operation_id),
            }
            stop = args.stop_after
            try:
                dry = await service.dry_run(
                    request=DryRunRequest(operation_id=operation_id, plan_ids=ids),
                    principal=principal,
                )
                line["issues"] = sorted({issue.code.value for issue in dry.issues})
                if not dry.valid:
                    tally.record({**line, "outcome": "refused"}, failed=True, stop_after=stop)
                elif not args.execute:
                    tally.record({**line, "outcome": "would_deploy"}, failed=False, stop_after=stop)
                else:
                    detail = await service.deploy(
                        request=DeployRequest(operation_id=operation_id, plan_ids=ids),
                        principal=principal,
                    )
                    failed = detail.status in {
                        DeploymentStatus.FAILED,
                        DeploymentStatus.CONFLICT,
                        DeploymentStatus.VERIFICATION_FAILED,
                    }
                    tally.record(
                        {
                            **line,
                            "outcome": detail.status.value,
                            "deployment_id": str(detail.id),
                            "replay": detail.idempotent_replay,
                            "error_code": detail.error_code,
                        },
                        failed=failed,
                        stop_after=stop,
                    )
            except Exception as exc:
                codes = sorted({i.code.value for i in getattr(exc, "issues", [])})
                tally.record(
                    {**line, "outcome": "error", "error": type(exc).__name__, "issues": codes},
                    failed=True,
                    stop_after=stop,
                )
            if tally.tripped:
                break
            if args.execute and args.pause_s > 0:
                await asyncio.sleep(args.pause_s)
    return _finish("apply", tally, len(selected), len(grouped), args.out)


def _codes(exc: Exception) -> set[str]:
    return {str(getattr(i.code, "value", i.code)) for i in getattr(exc, "issues", [])}


async def _rollback_one(
    service: DatabaseDeploymentService,
    principal: Principal,
    deployment_id: uuid.UUID,
    args: argparse.Namespace,
) -> tuple[str, bool]:
    request = RollbackRequest(
        operation_id=rollback_operation_id(deployment_id),
        reason=RollbackReason(args.reason),
        note=args.note,
    )
    outcome = "error"
    for attempt in range(ROLLBACK_ATTEMPTS):
        try:
            detail = await service.rollback(
                deployment_id=deployment_id, request=request, principal=principal
            )
            return detail.status.value, detail.status is not DeploymentStatus.ROLLED_BACK
        except Exception as exc:
            # A refusal other than "in progress" is a decision, not a fault: do not retry it.
            if hasattr(exc, "issues") and "deployment_in_progress" not in _codes(exc):
                return "refused", True
            outcome = "in_progress" if hasattr(exc, "issues") else f"error:{type(exc).__name__}"
        if attempt < ROLLBACK_ATTEMPTS - 1:
            await asyncio.sleep(ROLLBACK_BACKOFF_S[min(attempt, len(ROLLBACK_BACKOFF_S) - 1)])
    return outcome, True


async def _rollback(
    service: DatabaseDeploymentService,
    sessionmaker: async_sessionmaker[AsyncSession],
    principal: Principal,
    args: argparse.Namespace,
) -> int:
    targets = (await live_deployments(sessionmaker, args.ids))[: args.max_deployments]
    tally = Tally()
    for deployment_id in targets:
        if tally.tripped:
            tally.skipped += 1
            continue
        line: dict[str, Any] = {
            "command": "rollback",
            "deployment_id": str(deployment_id),
            "executed": args.execute,
        }
        if not args.execute:
            tally.record(
                {**line, "outcome": "would_roll_back"}, failed=False, stop_after=args.stop_after
            )
            continue
        outcome, failed = await _rollback_one(service, principal, deployment_id, args)
        tally.record({**line, "outcome": outcome}, failed=failed, stop_after=args.stop_after)
    return _finish("rollback", tally, len(targets), len(targets), args.out)


def _finish(command: str, tally: Tally, selected: int, available: int, out: Path | None) -> int:
    summary = {
        "command": command,
        "summary": True,
        "selected": selected,
        "available": available,
        "ok": tally.ok,
        "failed": tally.failed,
        "skipped_after_breaker": tally.skipped,
        "breaker_tripped": tally.tripped,
    }
    print(json.dumps(summary, sort_keys=True))
    if out is not None:
        out.write_text(
            json.dumps({"summary": summary, "items": tally.lines}, indent=1, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return EXIT_RUN_ERRORS if tally.failed or tally.tripped else EXIT_OK


async def _run(settings: Settings, args: argparse.Namespace) -> int:
    engine = create_engine(settings)
    sessionmaker = create_sessionmaker(engine)
    cms = None
    try:
        if args.command == "status":
            return await _status(sessionmaker)
        if args.execute and not settings.cms_writes_enabled:
            print(WRITES_REFUSAL, file=sys.stderr)
            return EXIT_USAGE
        principal = await _principal(sessionmaker, args.as_user)
        if principal is None:
            print("refused: unknown, disabled or non-deploying user", file=sys.stderr)
            return EXIT_USAGE
        cms = build_cms_provider(settings)
        service = build_deployment_service(sessionmaker=sessionmaker, settings=settings, cms=cms)
        if args.command == "apply":
            return await _apply(service, sessionmaker, principal, args)
        return await _rollback(service, sessionmaker, principal, args)
    except (ConfigurationError, ValueError) as exc:
        print(f"configuration error ({type(exc).__name__})", file=sys.stderr)
        return EXIT_USAGE
    finally:
        if cms is not None:
            await cms.aclose()
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code not in (0, None) else EXIT_OK
    if getattr(args, "stop_after", 1) < 1:
        print("usage error: --stop-after must be at least 1", file=sys.stderr)
        return EXIT_USAGE
    try:
        settings = get_settings()
    except (ValidationError, ConfigurationError):
        print("configuration error: invalid KEN_* settings", file=sys.stderr)
        return EXIT_USAGE
    ken_logging.configure_logging(level=settings.log_level, fmt=settings.log_format)
    return asyncio.run(_run(settings, args))


if __name__ == "__main__":
    raise SystemExit(main())
