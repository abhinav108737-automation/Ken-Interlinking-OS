#!/usr/bin/env python
"""Thin entry point for the local end-to-end demo (``make demo``; docs/DEMO.md).

    .venv/bin/python infrastructure/scripts/demo.py          # run the demo
    .venv/bin/python infrastructure/scripts/demo.py clean    # drop the demo databases + collection
    .venv/bin/python infrastructure/scripts/demo.py --ollama # opt in to real embeddings

The implementation lives in ``services/ken_services/demo`` so it is type-checked like the rest
of the services.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for _root in ("contracts/python", "services", "agents"):
    _path = str(REPO_ROOT / _root)
    if _path not in sys.path:
        sys.path.insert(0, _path)


def main() -> int:
    from ken_services.demo.__main__ import main as demo_main

    return demo_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
