"""Regenerate the frozen OpenAPI contract: ``make openapi``.

Writes contracts/openapi/openapi.json (sorted keys, 2-space indent, trailing
newline) from the live application, with settings isolated from any .env.
Regenerate apps/web/lib/types.gen.ts afterwards (``make openapi`` does both).
The snapshot test (tests/contract/test_openapi_snapshot.py) fails until the
committed file matches the code, so drift is always a deliberate change.
"""

from __future__ import annotations

import json
from pathlib import Path

from ken_api.main import create_app
from ken_core.config import Settings

TARGET = Path(__file__).resolve().parents[2] / "contracts" / "openapi" / "openapi.json"


def main() -> None:
    document = create_app(Settings(_env_file=None)).openapi()
    # newline="\n" explicitly: Path.write_text translates "\n" to "\r\n" on Windows, so a
    # regeneration there rewrote all 17,716 lines and buried the real change in the diff.
    TARGET.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"wrote {TARGET.relative_to(TARGET.parents[2])}")


if __name__ == "__main__":
    main()
