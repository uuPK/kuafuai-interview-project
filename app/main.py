"""ASGI application factory: load settings once at process startup."""

from fastapi import FastAPI

from app.api.health import router as health_router
from app.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings if settings is not None else Settings()
    application = FastAPI(title=configured.app_name)
    application.state.settings = configured
    application.include_router(health_router)
    return application
