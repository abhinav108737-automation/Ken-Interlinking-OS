"""Custom column types.

There is deliberately NO vector type here. ADR-0017 removed the vector extension:
embeddings are stored
as a plain PostgreSQL ``real[]`` (``postgresql.ARRAY(REAL)``, which needs no custom type and
no extension), and similarity search happens in the vector store
(``ken_contracts.vectors.VectorStore``), never in SQL. The column stays dimension-less -- the
dimension is whatever the configured EMBEDDING model reports at runtime
(``EmbeddingResult.dimensions``), stored per row and CHECKed with ``array_length``. Nothing
hardcodes 768.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.engine import Dialect
from sqlalchemy.types import UserDefinedType


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
