"""FastAPI dependencies.

Everything a route needs is resolved here from ``app.state``, which is
populated by ``create_app`` (settings, identity) and by the lifespan
(sessionmaker, providers). Tests replace any of these with
``app.dependency_overrides`` or by pre-seeding ``app.state`` -- no route ever
reaches for a module-level global.
"""

from __future__ import annotations

import importlib
from typing import Annotated, Any, Final, cast

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.agents import AgentReader
from ken_contracts.cms import CMSProvider
from ken_contracts.common import ServiceIdentity
from ken_contracts.intelligence import IntelligenceReader
from ken_contracts.inventory import InventoryReader
from ken_contracts.jobs import JobReader
from ken_contracts.model import ModelProvider
from ken_contracts.recommendations import RecommendationReader
from ken_core.config import Settings
from ken_core.health.probes import Probe, build_probes

INVENTORY_READER: Final = "ken_services.inventory.reader"
INTELLIGENCE_READER: Final = "ken_services.intelligence.reader"
JOB_READER: Final = "ken_services.jobs.reader"
RECOMMENDATION_READER: Final = "ken_services.recommendations.reader"
AGENT_READER: Final = "ken_services.agents.reader"


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
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
) -> InventoryReader:
    """The SQL-backed reader. Resolved by name, like the provider factories in main.py."""
    module: Any = importlib.import_module(INVENTORY_READER)
    reader: InventoryReader = module.SqlInventoryReader(sessionmaker)
    return reader


def get_intelligence_reader(
    settings: Annotated[Settings, Depends(get_app_settings)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
    model_provider: Annotated[ModelProvider, Depends(get_model_provider)],
) -> IntelligenceReader:
    module: Any = importlib.import_module(INTELLIGENCE_READER)
    reader: IntelligenceReader = module.SqlIntelligenceReader(
        sessionmaker=sessionmaker, model_provider=model_provider, settings=settings
    )
    return reader


def get_job_reader(
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
) -> JobReader:
    module: Any = importlib.import_module(JOB_READER)
    reader: JobReader = module.SqlJobReader(sessionmaker)
    return reader


def get_recommendation_reader(
    settings: Annotated[Settings, Depends(get_app_settings)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
) -> RecommendationReader:
    module: Any = importlib.import_module(RECOMMENDATION_READER)
    reader: RecommendationReader = module.SqlRecommendationReader(
        sessionmaker=sessionmaker, settings=settings
    )
    return reader


def get_agent_reader(
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
) -> AgentReader:
    module: Any = importlib.import_module(AGENT_READER)
    reader: AgentReader = module.DatabaseAgentReader(sessionmaker=sessionmaker)
    return reader


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
IdentityDep = Annotated[ServiceIdentity, Depends(get_identity)]
ProbesDep = Annotated[list[Probe], Depends(get_probes)]
InventoryReaderDep = Annotated[InventoryReader, Depends(get_inventory_reader)]
IntelligenceReaderDep = Annotated[IntelligenceReader, Depends(get_intelligence_reader)]
JobReaderDep = Annotated[JobReader, Depends(get_job_reader)]
RecommendationReaderDep = Annotated[RecommendationReader, Depends(get_recommendation_reader)]
AgentReaderDep = Annotated[AgentReader, Depends(get_agent_reader)]
