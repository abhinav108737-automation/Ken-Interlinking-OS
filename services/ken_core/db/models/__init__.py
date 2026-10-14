"""ORM models.

Every model must be imported here so that ``Base.metadata`` is complete when
Alembic's env.py imports this package for autogenerate comparison.
"""

from ken_core.db.models.agents import AgentRun, AgentRunPlan, AgentStep
from ken_core.db.models.event import Event
from ken_core.db.models.intelligence import (
    JobRun,
    PageAnalysis,
    PageEmbedding,
    PageProtection,
    PageRelationship,
)
from ken_core.db.models.inventory import (
    ExistingLink,
    IngestionIssue,
    IngestionRun,
    KnownUrl,
    Page,
    PageVersion,
)
from ken_core.db.models.recommendations import (
    LinkCandidate,
    LinkPlan,
    LinkPlanTransition,
    Opportunity,
)

__all__ = [
    "AgentRun",
    "AgentRunPlan",
    "AgentStep",
    "Event",
    "ExistingLink",
    "IngestionIssue",
    "IngestionRun",
    "JobRun",
    "KnownUrl",
    "LinkCandidate",
    "LinkPlan",
    "LinkPlanTransition",
    "Opportunity",
    "Page",
    "PageAnalysis",
    "PageEmbedding",
    "PageProtection",
    "PageRelationship",
    "PageVersion",
]
