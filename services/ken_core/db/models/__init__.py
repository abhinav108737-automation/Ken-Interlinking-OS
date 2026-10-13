"""ORM models.

Every model must be imported here so that ``Base.metadata`` is complete when
Alembic's env.py imports this package for autogenerate comparison.
"""

from ken_core.db.models.event import Event

__all__ = ["Event"]
