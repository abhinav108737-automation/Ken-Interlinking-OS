"""API routers. Phase 0 exposes health, readiness and meta only -- all GET."""

from ken_api.routers.health import router as health_router

__all__ = ["health_router"]
