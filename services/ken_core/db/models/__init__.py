"""ORM models.

Every model must be imported here so that ``Base.metadata`` is complete when
Alembic's env.py imports this package for autogenerate comparison.
"""

from ken_core.db.models.agents import AgentRun, AgentRunPlan, AgentStep
from ken_core.db.models.analytics import (
    AnalyticsImport,
    LinkEvent,
    LinkMeasurementRow,
    LinkMetricsDaily,
    PageMetricsDaily,
)
from ken_core.db.models.deployment import Deployment, DeploymentItem, DeploymentVerification
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
from ken_core.db.models.learning import (
    Experiment,
    ExperimentAssignment,
    LearningProposal,
    LearningProposalTransition,
    PolicyCandidate,
    PolicyEvaluation,
    Skill,
)
from ken_core.db.models.recommendations import (
    LinkCandidate,
    LinkPlan,
    LinkPlanTransition,
    Opportunity,
)
from ken_core.db.models.review import AuthSession, ReviewActionRow, User

__all__ = [
    "AgentRun",
    "AgentRunPlan",
    "AgentStep",
    "AnalyticsImport",
    "AuthSession",
    "Deployment",
    "DeploymentItem",
    "DeploymentVerification",
    "Event",
    "ExistingLink",
    "Experiment",
    "ExperimentAssignment",
    "IngestionIssue",
    "IngestionRun",
    "JobRun",
    "KnownUrl",
    "LearningProposal",
    "LearningProposalTransition",
    "LinkCandidate",
    "LinkEvent",
    "LinkMeasurementRow",
    "LinkMetricsDaily",
    "LinkPlan",
    "LinkPlanTransition",
    "Opportunity",
    "Page",
    "PageAnalysis",
    "PageEmbedding",
    "PageMetricsDaily",
    "PageProtection",
    "PageRelationship",
    "PageVersion",
    "PolicyCandidate",
    "PolicyEvaluation",
    "ReviewActionRow",
    "Skill",
    "User",
]
