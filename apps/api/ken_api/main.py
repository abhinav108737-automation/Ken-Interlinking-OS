"""FastAPI application factory.

``create_app`` does no I/O beyond reading the package version and the git SHA
once, so that ``GET /health`` can answer from memory. Everything that touches
a dependency (engine, providers) is created in the lifespan and released on
shutdown.

Provider factories are imported lazily, by name, inside the lifespan. They are
owned by other workstreams; resolving them at call time keeps this module
importable (and its tests runnable) independently of them.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, Final

from fastapi import FastAPI
from pydantic import TypeAdapter

from ken_api.errors import install_exception_handlers
from ken_api.middleware.request_id import RequestIdMiddleware
from ken_api.routers.health import router as health_router
from ken_contracts.cms import CMSProvider
from ken_contracts.common import ServiceIdentity
from ken_contracts.errors import ErrorResponse
from ken_contracts.model import ModelProvider
from ken_core import logging as ken_logging
from ken_core.config import Settings, get_settings, git_sha
from ken_core.db.engine import create_engine
from ken_core.db.session import create_sessionmaker

API_TITLE: Final = "Ken Interlink OS API"
DISTRIBUTION: Final = "ken-interlink"
MODEL_FACTORY: Final = "ken_core.providers.model.factory"
CMS_FACTORY: Final = "ken_core.providers.cms.factory"

logger = ken_logging.get_logger(__name__)


def _app_version() -> str:
    try:
        return importlib.metadata.version(DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0+unknown"


def _build_model_provider(settings: Settings) -> ModelProvider:
    factory: Any = importlib.import_module(MODEL_FACTORY)
    provider: ModelProvider = factory.build_model_provider(settings)
    return provider


def _build_cms_provider(settings: Settings) -> CMSProvider:
    factory: Any = importlib.import_module(CMS_FACTORY)
    provider: CMSProvider = factory.build_cms_provider(settings)
    return provider


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create dependencies on startup; release the ones this lifespan created.

    Anything already present on ``app.state`` (a test fake, say) is used as-is
    and NOT closed here: whoever created it owns it.
    """
    settings: Settings = app.state.settings
    ken_logging.configure_logging(level=settings.log_level, fmt=settings.log_format)

    async with AsyncExitStack() as stack:
        if getattr(app.state, "sessionmaker", None) is None:
            engine = create_engine(settings)  # lazy: no connection is opened here
            stack.push_async_callback(engine.dispose)
            app.state.engine = engine
            app.state.sessionmaker = create_sessionmaker(engine)

        if getattr(app.state, "model_provider", None) is None:
            model_provider = _build_model_provider(settings)
            stack.push_async_callback(model_provider.aclose)
            app.state.model_provider = model_provider

        if getattr(app.state, "cms_provider", None) is None:
            cms_provider = _build_cms_provider(settings)
            stack.push_async_callback(cms_provider.aclose)
            app.state.cms_provider = cms_provider

        logger.info(
            "api_started",
            service=settings.service_name,
            environment=settings.environment,
            phase=settings.phase,
            model_provider=app.state.model_provider.name,
            cms_provider=app.state.cms_provider.name,
        )
        yield
        logger.info("api_stopping")


def _install_openapi(app: FastAPI) -> None:
    """Publish ``ErrorResponse`` (and what it references) as a component schema.

    No Phase 0 route declares an error response, so FastAPI would otherwise
    omit the schema -- yet every error body this API returns has that shape
    (see ken_api.errors), and the frozen contract lists it so that clients can
    generate the type before Phase 1 routes reference it.
    """
    default_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = default_openapi()
        error_schema = TypeAdapter(ErrorResponse).json_schema(
            mode="serialization", ref_template="#/components/schemas/{model}"
        )
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        for name, definition in error_schema.pop("$defs", {}).items():
            components.setdefault(name, definition)
        components.setdefault(ErrorResponse.__name__, error_schema)
        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # type: ignore[method-assign]


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the API. Pass ``settings`` explicitly in tests."""
    settings = settings or get_settings()
    identity = ServiceIdentity(
        service=settings.service_name,
        version=_app_version(),
        environment=settings.environment,
        git_sha=git_sha(),
    )

    # Interactive docs and the live schema are development aids, not part of
    # the Phase 0 surface; the published contract is contracts/openapi/openapi.json.
    dev_docs = settings.environment in {"local", "ci"}
    app = FastAPI(
        title=API_TITLE,
        version=identity.version,
        lifespan=lifespan,
        docs_url="/docs" if dev_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if dev_docs else None,
    )
    app.state.settings = settings
    app.state.identity = identity
    app.state.started_monotonic = time.monotonic()

    app.add_middleware(RequestIdMiddleware)
    install_exception_handlers(app)
    app.include_router(health_router)
    _install_openapi(app)
    return app


app = create_app()
