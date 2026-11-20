"""Priority-designation command line (Business Phase 2A-1). LOCAL management + shadow only.

``python -m ken_services.recommendations.priority_cli <command>``:

* ``add``     -- designate a page (``--actor`` required: every change is attributable)
* ``retire``  -- record when a designation stops being in force. ``--at`` may be in the FUTURE,
  which schedules the retirement and is how an open-ended designation is replaced on a date
  without editing it. Idempotent; nothing is ever edited in place.
* ``list``    -- list designations; those with a retirement recorded are hidden unless ``--all``
* ``shadow``  -- report what priority WOULD have changed in one recommendation run.
  Read-only: it writes no row anywhere. ``--out`` writes the report as JSON to a local file.

Nothing here changes a live score, a plan, a module or a page: the shadow is a report, and the
designations it reads have no effect on the engine until the scoring ADR (owner decision 3)
is accepted and a ``priority`` ranking component exists.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from ken_contracts.intelligence import analysis_version
from ken_core import logging as ken_logging
from ken_core.config import ConfigurationError, Settings, get_settings
from ken_core.db.engine import create_engine
from ken_core.db.session import create_sessionmaker
from ken_core.priority import MAX_TIER, MIN_TIER, PriorityType
from ken_services.intelligence.taxonomy import load_taxonomy
from ken_services.recommendations.priority import PriorityDesignation
from ken_services.recommendations.priority_shadow import ShadowReport
from ken_services.recommendations.priority_store import (
    DesignationNotFoundError,
    RunNotFoundError,
    ShadowRunContext,
    add_designation,
    build_shadow_report,
    latest_recommendation_run,
    list_designations,
    retire_designation,
    violated_constraint,
)

__all__ = ["main", "report_json"]

EXIT_OK: Final = 0
EXIT_REFUSED: Final = 1
EXIT_USAGE: Final = 2


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a UUID: {value!r}") from exc


def _instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an ISO-8601 instant: {value!r}") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError(f"instant must carry a timezone: {value!r}")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="priority", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    add = commands.add_parser("add", help="Designate a page as a business priority.")
    add.add_argument("--page-id", type=_uuid, required=True)
    add.add_argument("--type", choices=[t.value for t in PriorityType], required=True)
    add.add_argument("--tier", type=int, choices=range(MIN_TIER, MAX_TIER + 1), required=True)
    add.add_argument("--reason", required=True)
    add.add_argument("--owner", required=True)
    add.add_argument("--effective-from", type=_instant, default=None, help="default: now")
    add.add_argument("--effective-to", type=_instant, default=None, help="default: open-ended")
    add.add_argument("--actor", required=True, help="who is making this change")

    retire = commands.add_parser("retire", help="Record when a designation stops applying.")
    retire.add_argument("--id", type=_uuid, required=True)
    retire.add_argument("--actor", required=True)
    retire.add_argument(
        "--at", type=_instant, default=None,
        help="when it stops applying; may be in the future to SCHEDULE it. Default: now",
    )  # fmt: skip

    listing = commands.add_parser("list", help="List designations.")
    listing.add_argument("--page-id", type=_uuid, default=None)
    listing.add_argument("--all", action="store_true", help="include retired designations")

    shadow = commands.add_parser("shadow", help="Shadow one recommendation run (read-only).")
    shadow.add_argument("--run-id", type=_uuid, default=None, help="default: latest succeeded")
    shadow.add_argument("--at", type=_instant, default=None, help="default: now")
    shadow.add_argument("--out", type=Path, default=None, help="write the report as JSON here")
    return parser


def _designation_json(d: PriorityDesignation) -> dict[str, Any]:
    return {
        "id": str(d.id),
        "page_id": str(d.page_id),
        "priority_type": d.priority_type.value,
        "tier": d.tier,
        "weight": d.weight,
        "reason": d.reason,
        "owner": d.owner,
        "effective_from": d.effective_from.isoformat(),
        "effective_to": None if d.effective_to is None else d.effective_to.isoformat(),
        "retired_at": None if d.retired_at is None else d.retired_at.isoformat(),
        "validity_end": None if d.validity_end is None else d.validity_end.isoformat(),
        "in_force_now": d.is_effective_at(datetime.now(UTC)),
        "created_at": d.created_at.isoformat(),
        "created_by": d.created_by,
        "updated_at": d.updated_at.isoformat(),
        "updated_by": d.updated_by,
    }


def report_json(context: ShadowRunContext, report: ShadowReport) -> dict[str, Any]:
    """The report as JSON-ready data: inputs first, then summary counts, then rows."""
    return {
        "report_version": report.report_version,
        "shadowed_run": {
            "run_id": str(context.run_id),
            "started_at": context.started_at.isoformat(),
            "analysis_version": context.analysis_version,
            "policy_version": context.policy_version,
            "ranking_policy_version": context.ranking_policy_version,
        },
        "at": report.at.isoformat(),
        "assumed_weight": report.assumed_weight,
        "formula": report.formula,
        "live_scores_changed": False,
        "summary": {
            "qualified_candidates": len(report.rows),
            "sources": len({r.source_page_id for r in report.rows}),
            "designated": len(report.designated),
            "refused_by_eligibility": len(report.refused),
            "rank_changes": len(report.moved),
            "plan_set_changes": len(report.selection_changes),
            "max_priority_share": round(report.max_priority_share, 6),
        },
        "rows": [
            {
                "source_page_id": str(r.source_page_id),
                "target_page_id": str(r.target_page_id),
                "target_url": r.target_url,
                "live_score": r.live_score,
                "priority_value": r.priority_value,
                "priority_tier": r.priority_tier,
                "priority_types": [t.value for t in r.priority_types],
                "contribution": r.contribution,
                "shadow_score": r.shadow_score,
                "live_rank": r.live_rank,
                "shadow_rank": r.shadow_rank,
                "delta_rank": r.delta_rank,
                "live_selected": r.live_selected,
                "shadow_selected": r.shadow_selected,
                "priority_share": r.priority_share,
                "ineligible_reasons": list(r.ineligible_reasons),
            }
            for r in report.rows
        ],
    }


async def _run(settings: Settings, args: argparse.Namespace) -> int:
    engine = create_engine(settings)
    sessions = create_sessionmaker(engine)
    now = datetime.now(UTC)
    try:
        async with sessions() as session:
            if args.command == "add":
                try:
                    designation = await add_designation(
                        session,
                        page_id=args.page_id,
                        priority_type=PriorityType(args.type),
                        tier=args.tier,
                        reason=args.reason,
                        owner=args.owner,
                        effective_from=args.effective_from or now,
                        effective_to=args.effective_to,
                        actor=args.actor,
                        now=now,
                    )
                    await session.commit()
                except ValueError as exc:
                    print(f"refused: {exc}", file=sys.stderr)
                    return EXIT_REFUSED
                except IntegrityError as exc:
                    await session.rollback()
                    constraint = violated_constraint(exc.orig or exc)
                    reason = (
                        "another designation of that type is already in force over part of "
                        "that interval; retire it (--at may be in the future) or start after "
                        "it ends"
                        if constraint == "page_priorities_no_overlap"
                        else "unknown page id"
                        if constraint == "fk_page_priorities_page_id_pages"
                        else f"the database refused the designation ({constraint or 'unknown'})"
                    )
                    print(f"refused: {reason}", file=sys.stderr)
                    return EXIT_REFUSED
                print(json.dumps(_designation_json(designation), indent=2))
                return EXIT_OK

            if args.command == "retire":
                try:
                    designation = await retire_designation(
                        session,
                        designation_id=args.id,
                        actor=args.actor,
                        now=now,
                        at=args.at,
                    )
                    await session.commit()
                except (DesignationNotFoundError, ValueError) as exc:
                    print(f"refused: {type(exc).__name__}: {exc}", file=sys.stderr)
                    return EXIT_REFUSED
                print(json.dumps(_designation_json(designation), indent=2))
                return EXIT_OK

            if args.command == "list":
                designations = await list_designations(
                    session, page_id=args.page_id, include_retired=args.all
                )
                print(json.dumps([_designation_json(d) for d in designations], indent=2))
                return EXIT_OK

            # shadow -- read-only
            taxonomy = load_taxonomy(settings.taxonomy_path)
            run_id = args.run_id or await latest_recommendation_run(session)
            if run_id is None:
                print("refused: no succeeded recommendation run to shadow", file=sys.stderr)
                return EXIT_REFUSED
            try:
                context, report = await build_shadow_report(
                    session,
                    run_id=run_id,
                    taxonomy=taxonomy,
                    fallback_analysis_version=analysis_version(taxonomy.version),
                    at=args.at or now,
                )
            except RunNotFoundError as exc:
                print(f"refused: not a recommendation run: {exc}", file=sys.stderr)
                return EXIT_REFUSED
            await session.rollback()  # nothing was written; make that explicit
            print(report.summary())
            if args.out is not None:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_bytes(
                    (json.dumps(report_json(context, report), indent=2) + "\n").encode("utf-8")
                )
                print(f"wrote {args.out}")
            return EXIT_OK
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:  # argparse has already printed its usage message
        return EXIT_USAGE if exc.code not in (0, None) else EXIT_OK
    try:
        settings = get_settings()
    except (ValidationError, ConfigurationError):
        print("configuration error: invalid KEN_* settings", file=sys.stderr)
        return EXIT_USAGE
    ken_logging.configure_logging(level=settings.log_level, fmt=settings.log_format)
    return asyncio.run(_run(settings, args))


if __name__ == "__main__":
    raise SystemExit(main())
