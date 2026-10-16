"""Learning command line: ``python -m ken_services.learning.cli`` (ADR-0016 section 10).

CLI-ONLY, deliberately: no HTTP route starts a model run, writes a policy file or triggers
egress. The API has GET routes plus the one write route that RECORDS a stage change.

Subcommands land per workstream; this file currently implements W1's::

    reflect [--start YYYY-MM-DD --end YYYY-MM-DD] [--no-model]
    eval --suites <names|all> [--proposal UUID] [--skill UUID] [--baseline UUID]
         [--seed N] [--provider mock|ollama] [--report PATH] [--no-db]

``reflect`` runs one ``JobKind.REFLECTION`` job: it aggregates the editorial, behavioural and
search streams per decision class, enumerates candidate policy changes deterministically, asks
a bounded REASONING model to pick one (``--no-model`` forces the deterministic rule), and
inserts ``proposed`` rows. It promotes nothing and can promote nothing.

``eval`` runs one ``JobKind.EVALUATION`` job over the versioned datasets under
``evals/datasets/``, writes a canonical JSON report plus (when the run names a proposal or a
skill) one append-only ``policy_evaluations`` row, and evaluates the gate. Its own parser lives
in :mod:`ken_services.learning.evaluate`; this file only forwards the arguments. It activates
nothing, and a live ``--provider ollama`` run can never pass a gate.

Prints ONE JSON line. Exit codes: 0 done; 1 the run was refused or failed; 2 usage or
configuration error (nothing ran). It never prints a database URL, a model name resolved from a
secret, a file path or exception text.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import date, datetime
from typing import Final

from pydantic import ValidationError

from ken_contracts.learning import EvidenceWindow
from ken_core import logging as ken_logging
from ken_core.config import ConfigurationError, Settings, get_settings
from ken_core.db.engine import create_engine
from ken_core.db.session import create_sessionmaker
from ken_core.providers.model.factory import build_model_provider
from ken_services.jobs.runner import JobAlreadyRunningError
from ken_services.learning.policy import PolicyError, load_configured_learning_policy
from ken_services.learning.protocol import ReflectionOutcome, ReflectionRequest
from ken_services.learning.reflect import SkillReflectionUnsupportedError, run_reflection

__all__ = ["EXIT_OK", "EXIT_RUN_ERROR", "EXIT_USAGE", "main", "reflect_line"]

EXIT_OK: Final = 0
EXIT_RUN_ERROR: Final = 1
EXIT_USAGE: Final = 2


def _iso_date(value: str) -> date:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError("must be a date as YYYY-MM-DD") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ken_services.learning.cli",
        description="Learning: reflection and proposals (ADR-0016).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    reflect = commands.add_parser(
        "reflect", help="Corpus-level reflection -> learning proposals (promotes nothing)."
    )
    reflect.add_argument("--start", default=None, type=_iso_date)
    reflect.add_argument("--end", default=None, type=_iso_date)
    reflect.add_argument(
        "--no-model",
        dest="use_model",
        action="store_false",
        help="Force the deterministic rule (option 0) and record the fallback.",
    )
    reflect.set_defaults(use_model=True)
    # `eval` is registered so that `--help` lists it, but it is DISPATCHED BEFORE this parser
    # runs (see `main`): its own argparse lives in ken_services.learning.evaluate, which
    # `make eval`, `evals/runner.py` and the tests all drive, and argparse cannot forward
    # option-shaped arguments to a subparser reliably.
    commands.add_parser(
        "eval",
        help="Offline evaluation over the versioned eval datasets: "
        "eval --suites <names|all> [--proposal UUID] [--skill UUID] [--provider mock|ollama]. "
        "Writes a report and activates nothing.",
        add_help=False,
    )
    return parser


def reflect_line(outcome: ReflectionOutcome) -> str:
    """One JSON line describing the run. Counts and ids only."""
    return json.dumps(
        {
            "command": "reflect",
            "job_run_id": str(outcome.job_run_id),
            "proposals": [str(item) for item in outcome.proposal_ids],
            "deduplicated": outcome.deduplicated,
            "counters": dict(sorted(outcome.counters.items())),
        },
        sort_keys=True,
    )


async def _reflect(settings: Settings, args: argparse.Namespace) -> int:
    if (args.start is None) != (args.end is None):
        print("configuration error: give both --start and --end, or neither", file=sys.stderr)
        return EXIT_USAGE
    window = None
    if args.start is not None and args.end is not None:
        try:
            window = EvidenceWindow(start=args.start, end=args.end)
        except ValidationError:
            print("configuration error: invalid observation window", file=sys.stderr)
            return EXIT_USAGE
    try:
        policy = load_configured_learning_policy(settings)
    except (PolicyError, FileNotFoundError, OSError):
        print("configuration error: the learning policy could not be loaded", file=sys.stderr)
        return EXIT_USAGE

    engine = create_engine(settings)
    provider = build_model_provider(settings) if args.use_model else None
    try:
        outcome = await run_reflection(
            sessionmaker=create_sessionmaker(engine),
            settings=settings,
            policy=policy,
            request=ReflectionRequest(window=window, use_model=bool(args.use_model)),
            model_provider=provider,
        )
    except SkillReflectionUnsupportedError:
        print("configuration error: reflection proposes policies only", file=sys.stderr)
        return EXIT_USAGE
    except JobAlreadyRunningError:
        print("another reflection run is in progress", file=sys.stderr)
        return EXIT_RUN_ERROR
    except Exception as exc:
        print(f"the reflection could not be completed ({type(exc).__name__})", file=sys.stderr)
        return EXIT_RUN_ERROR
    finally:
        if provider is not None:
            try:
                await provider.aclose()
            except Exception as exc:
                print(f"provider close failed ({type(exc).__name__})", file=sys.stderr)
        await engine.dispose()
    print(reflect_line(outcome))
    return EXIT_OK


async def _run(settings: Settings, args: argparse.Namespace) -> int:
    match args.command:
        case "reflect":
            return await _reflect(settings, args)
        case _:  # pragma: no cover - argparse requires a known command
            return EXIT_USAGE


def _eval(arguments: Sequence[str]) -> int:
    """``make eval SUITES=... [PROPOSAL=...] [SKILL=...]``.

    Delegated whole to :func:`ken_services.learning.evaluate.eval_main`, which owns the eval
    parser, the gate and the report, and which runs its own event loop. Dispatched before this
    module's parser touches ``argv``, so the eval flags are parsed in exactly one place.
    """
    from ken_services.learning.evaluate import eval_main

    return eval_main(list(arguments))


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "eval":
        return _eval(arguments[1:])
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


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
