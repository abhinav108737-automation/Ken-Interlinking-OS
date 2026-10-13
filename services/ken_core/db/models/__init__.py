"""ORM models.

Every model must be imported here so that ``Base.metadata`` is complete when
Alembic's env.py imports this package for autogenerate comparison.
"""

from ken_core.db.models.event import Event
from ken_core.db.models.inventory import (
    ExistingLink,
    IngestionIssue,
    IngestionRun,
    KnownUrl,
    Page,
    PageVersion,
)

__all__ = [
    "Event",
    "ExistingLink",
    "IngestionIssue",
    "IngestionRun",
    "KnownUrl",
    "Page",
    "PageVersion",
]
