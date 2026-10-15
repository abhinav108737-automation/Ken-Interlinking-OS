"""FastAPI dependencies.

Everything a route needs is resolved here from ``app.state``, which is
populated by ``create_app`` (settings, identity) and by the lifespan
(sessionmaker, providers). Tests replace any of these with
``app.dependency_overrides`` or by pre-seeding ``app.state`` -- no route ever
reaches for a module-level global.
"""

from __future__ import annotations

import importlib
import uuid
from typing import Annotated, Any, Final, Protocol, cast

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.agents import AgentReader
from ken_contracts.analytics import AnalyticsReader
from ken_contracts.auth import AuthProvider
from ken_contracts.cms import CMSProvider
from ken_contracts.common import ServiceIdentity
from ken_contracts.controlplane import DashboardSummary, EventRecord, PoliciesView
from ken_contracts.deployment import DeploymentReader, DeploymentService
from ken_contracts.errors import ListResponse
from ken_contracts.events import EventName
from ken_contracts.intelligence import IntelligenceReader
from ken_contracts.inventory import InventoryReader
from ken_contracts.jobs import JobReader
from ken_contracts.model import ModelProvider
from ken_contracts.recommendations import RecommendationReader
from ken_contracts.review import ReviewReader, ReviewService, TargetOption
from ken_core.config import Settings
from ken_core.health.probes import Probe, build_probes

INVENTORY_READER: Final = "ken_services.inventory.reader"
INTELLIGENCE_READER: Final = "ken_services.intelligence.reader"
JOB_READER: Final = "ken_services.jobs.reader"
RECOMMENDATION_READER: Final = "ken_services.recommendations.reader"
AGENT_READER: Final = "ken_services.agents.reader"
REVIEW_SERVICE: Final = "ken_services.review.service"
REVIEW_READER: Final = "ken_services.review.reader"
CONTROL_PLANE_READER: Final = "ken_services.review.controlplane"
DEPLOYMENT_READER: Final = "ken_services.deployment.reader"
DEPLOYMENT_COMPOSITION: Final = "ken_services.deployment.compose"
ANALYTICS_READER: Final = "ken_services.analytics.reader"


class TargetOptionsService(ReviewService, Protocol):
    """The review service plus the replacement-destination picker (ADR-0013 s7)."""

    async def target_options(self, *, plan_id: uuid.UUID, limit: int) -> list[TargetOption]: ...


class ControlPlaneReader(Protocol):
    """``ken_services.review.controlplane.SqlControlPlaneReader`` (structural)."""

    async def dashboard(self) -> DashboardSummary: ...

    async def list_events(
        self,
        *,
        limit: int,
        offset: int,
        event_name: EventName | None = None,
        subject_id: str | None = None,
    ) -> ListResponse[EventRecord]: ...

    async def policies(self) -> PoliciesView: ...


class AppStateNotInitialisedError(RuntimeError):
    """A dependency was requested before the lifespan populated ``app.state``."""


def _state(request: Request, name: str) -> Any:
    value = getattr(request.app.state, name, None)
    if value is None:
        raise AppStateNotInitialisedError(f"app.state.{name} is not initialised")
    return value


def get_app_settings(request: Request) -> Settings:
    return cast(Settings, _state(request, "settings"))


def get_identity(request: Request) -> ServiceIdentity:
    return cast(ServiceIdentity, _state(request, "identity"))


def get_sessionmaker(request: Request) -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], _state(request, "sessionmaker"))


def get_readonly_sessionmaker(request: Request) -> async_sessionmaker[AsyncSession]:
    """The READ ONLY factory for GET readers (ADR-0014 s8): ``app.state.readonly_sessionmaker``
    when the lifespan (or a test) installed one, else derived once from the read-write
    factory's engine. Every transaction it opens is ``SET TRANSACTION READ ONLY``."""
    existing = getattr(request.app.state, "readonly_sessionmaker", None)
    if existing is not None:
        return cast(async_sessionmaker[AsyncSession], existing)
    from ken_core.db.session import create_readonly_sessionmaker

    bind = get_sessionmaker(request).kw.get("bind")
    if bind is None:  # pragma: no cover - every sessionmaker here is engine-bound
        raise AppStateNotInitialisedError("app.state.sessionmaker has no engine")
    readonly = create_readonly_sessionmaker(bind)
    request.app.state.readonly_sessionmaker = readonly
    return readonly


def get_model_provider(request: Request) -> ModelProvider:
    return cast(ModelProvider, _state(request, "model_provider"))


def get_cms_provider(request: Request) -> CMSProvider:
    return cast(CMSProvider, _state(request, "cms_provider"))


def get_probes(
    settings: Annotated[Settings, Depends(get_app_settings)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
    model_provider: Annotated[ModelProvider, Depends(get_model_provider)],
    cms_provider: Annotated[CMSProvider, Depends(get_cms_provider)],
) -> list[Probe]:
    return build_probes(
        settings=settings,
        sessionmaker=sessionmaker,
        model_provider=model_provider,
        cms_provider=cms_provider,
    )


def get_inventory_reader(
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_readonly_sessionmaker)],
) -> InventoryReader:
    """The SQL-backed reader. Resolved by name, like the provider factories in main.py."""
    module: Any = importlib.import_module(INVENTORY_READER)
    reader: InventoryReader = module.SqlInventoryReader(sessionmaker)
    return reader


def get_intelligence_reader(
    settings: Annotated[Settings, Depends(get_app_settings)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_readonly_sessionmaker)],
    model_provider: Annotated[ModelProvider, Depends(get_model_provider)],
) -> IntelligenceReader:
    module: Any = importlib.import_module(INTELLIGENCE_READER)
    reader: IntelligenceReader = module.SqlIntelligenceReader(
        sessionmaker=sessionmaker, model_provider=model_provider, settings=settings
    )
    return reader


def get_job_reader(
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_readonly_sessionmaker)],
) -> JobReader:
    module: Any = importlib.import_module(JOB_READER)
    reader: JobReader = module.SqlJobReader(sessionmaker)
    return reader


def get_recommendation_reader(
    settings: Annotated[Settings, Depends(get_app_settings)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_readonly_sessionmaker)],
) -> RecommendationReader:
    module: Any = importlib.import_module(RECOMMENDATION_READER)
    reader: RecommendationReader = module.SqlRecommendationReader(
        sessionmaker=sessionmaker, settings=settings
    )
    return reader


def get_agent_reader(
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_readonly_sessionmaker)],
) -> AgentReader:
    module: Any = importlib.import_module(AGENT_READER)
    reader: AgentReader = module.DatabaseAgentReader(sessionmaker=sessionmaker)
    return reader


def get_review_service(
    settings: Annotated[Settings, Depends(get_app_settings)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
) -> TargetOptionsService:
    module: Any = importlib.import_module(REVIEW_SERVICE)
    service: TargetOptionsService = module.DatabaseReviewService(
        sessionmaker=sessionmaker, settings=settings
    )
    return service


def get_review_reader(
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_readonly_sessionmaker)],
) -> ReviewReader:
    module: Any = importlib.import_module(REVIEW_READER)
    reader: ReviewReader = module.SqlReviewReader(sessionmaker=sessionmaker)
    return reader


def get_control_plane_reader(
    settings: Annotated[Settings, Depends(get_app_settings)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_readonly_sessionmaker)],
) -> ControlPlaneReader:
    module: Any = importlib.import_module(CONTROL_PLANE_READER)
    reader: ControlPlaneReader = module.SqlControlPlaneReader(
        sessionmaker=sessionmaker, settings=settings
    )
    return reader


def get_deployment_reader(
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_readonly_sessionmaker)],
) -> DeploymentReader:
    module: Any = importlib.import_module(DEPLOYMENT_READER)
    reader: DeploymentReader = module.SqlDeploymentReader(sessionmaker=sessionmaker)
    return reader


def get_deployment_service(
    request: Request,
    settings: Annotated[Settings, Depends(get_app_settings)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
    cms_provider: Annotated[CMSProvider, Depends(get_cms_provider)],
) -> DeploymentService:
    """``app.state.deployment_service`` if a caller installed one, else the database service
    over the configured CMS provider (read-write factory)."""
    installed = getattr(request.app.state, "deployment_service", None)
    if installed is not None:
        return cast(DeploymentService, installed)
    module: Any = importlib.import_module(DEPLOYMENT_COMPOSITION)
    service: DeploymentService = module.build_deployment_service(
        sessionmaker=sessionmaker, settings=settings, cms=cms_provider
    )
    return service


def get_analytics_reader(
    settings: Annotated[Settings, Depends(get_app_settings)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_readonly_sessionmaker)],
) -> AnalyticsReader:
    """``ken_services.analytics.reader.SqlAnalyticsReader`` over the READ ONLY factory (Phase 7,
    ADR-0015 s6). Resolved by name: until the backend workstream lands the module, an
    analytics route answers 500 (the staged-stub convention of earlier phases)."""
    module: Any = importlib.import_module(ANALYTICS_READER)
    reader: AnalyticsReader = module.SqlAnalyticsReader(
        sessionmaker=sessionmaker, settings=settings
    )
    return reader


def get_auth_provider_dep(request: Request) -> AuthProvider:
    from ken_api.security import get_auth_provider

    return get_auth_provider(request)


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
IdentityDep = Annotated[ServiceIdentity, Depends(get_identity)]
ProbesDep = Annotated[list[Probe], Depends(get_probes)]
InventoryReaderDep = Annotated[InventoryReader, Depends(get_inventory_reader)]
IntelligenceReaderDep = Annotated[IntelligenceReader, Depends(get_intelligence_reader)]
JobReaderDep = Annotated[JobReader, Depends(get_job_reader)]
RecommendationReaderDep = Annotated[RecommendationReader, Depends(get_recommendation_reader)]
AgentReaderDep = Annotated[AgentReader, Depends(get_agent_reader)]
ReviewServiceDep = Annotated[TargetOptionsService, Depends(get_review_service)]
ReviewReaderDep = Annotated[ReviewReader, Depends(get_review_reader)]
ControlPlaneReaderDep = Annotated[ControlPlaneReader, Depends(get_control_plane_reader)]
AuthProviderDep = Annotated[AuthProvider, Depends(get_auth_provider_dep)]
DeploymentReaderDep = Annotated[DeploymentReader, Depends(get_deployment_reader)]
DeploymentServiceDep = Annotated[DeploymentService, Depends(get_deployment_service)]
AnalyticsReaderDep = Annotated[AnalyticsReader, Depends(get_analytics_reader)]
