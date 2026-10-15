"""Custom column types.

``Vector`` maps pgvector's ``vector`` type without the ``pgvector`` Python
package. asyncpg has no codec for the extension type, so it exchanges it in
pgvector's TEXT form (``'[0.1,0.2,...]'``) in both directions -- verified
against the project image (ADR-0010). The column is deliberately declared
WITHOUT a fixed dimension: the dimension is whatever the configured EMBEDDING
model reports at runtime (``EmbeddingResult.dimensions``), stored per row, and
every similarity query filters on (model, dimensions). Nothing hardcodes 768.
"""

from __future__ import annotations

import math
from typing import Any

from sqlalchemy.engine import Dialect
from sqlalchemy.types import UserDefinedType


def format_vector(values: list[float] | tuple[float, ...]) -> str:
    """pgvector text literal. Rejects NaN/inf, which pgvector would reject anyway."""
    if not values:
        raise ValueError("a vector needs at least one dimension")
    parts: list[str] = []
    for value in values:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("vector components must be finite")
        parts.append(repr(number))
    return "[" + ",".join(parts) + "]"


def parse_vector(text: str) -> list[float]:
    body = text.strip()
    if not (body.startswith("[") and body.endswith("]")):
        raise ValueError("not a pgvector text literal")
    inner = body[1:-1].strip()
    return [float(part) for part in inner.split(",")] if inner else []


class Vector(UserDefinedType[list[float]]):
    """pgvector ``vector`` (dimension-less). Python value: ``list[float]``."""

    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return "vector"

    def bind_processor(self, dialect: Dialect) -> Any:
        def process(value: list[float] | tuple[float, ...] | None) -> str | None:
            return None if value is None else format_vector(value)

        return process

    def result_processor(self, dialect: Dialect, coltype: object) -> Any:
        def process(value: str | None) -> list[float] | None:
            return None if value is None else parse_vector(value)

        return process


class Xid8(UserDefinedType[str]):
    """PostgreSQL ``xid8`` (a 64-bit transaction id, ``pg_current_xact_id()``).

    Used only by ``link_plan_transitions.txid`` (migration 0007), which the ORM never writes
    (server default) and never loads (deferred): the status-integrity trigger compares it in
    SQL. The Python value, if ever read, is the decimal text form.
    """

    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return "xid8"

    def bind_processor(self, dialect: Dialect) -> Any:
        def process(value: str | int | None) -> str | None:
            return None if value is None else str(value)

        return process

    def result_processor(self, dialect: Dialect, coltype: object) -> Any:
        def process(value: object) -> str | None:
            return None if value is None else str(value)

        return process
