"""``python -m ken_services.demo [run|clean] [--ollama]`` (``make demo`` / ``make demo-clean``).

Exit codes: 0 every acceptance check passed; 1 at least one check failed or is missing;
2 the demo refused to run (a dependency is down, or a resource is not the demo's own).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from ken_services.demo.config import DemoConfig, DemoRefusalError
from ken_services.demo.console import Console
from ken_services.demo.runner import clean, run_demo

__all__ = ["main"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="demo", description="Repeatable local end-to-end demo of the Ken Interlinking OS."
    )
    parser.add_argument("command", nargs="?", choices=["run", "clean"], default="run")
    parser.add_argument(
        "--ollama",
        action="store_true",
        help="OPT-IN: use real embeddings from the local Ollama daemon (never required; "
        "the report is then not byte-reproducible).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = DemoConfig(ollama=bool(args.ollama))
    except DemoRefusalError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    if args.command == "clean":
        try:
            removed = asyncio.run(clean(config))
        except Exception as exc:
            print(f"demo-clean could not complete ({type(exc).__name__})", file=sys.stderr)
            return 1
        print(
            f"dropped databases {config.app_db}, {config.sim_db}; deleted collections: "
            f"{removed or 'none'}"
        )
        return 0
    out = Console()
    try:
        return asyncio.run(run_demo(config, out))
    except DemoRefusalError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
