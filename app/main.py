"""ASGI factory with shared, lazy runtime resources."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.config import Settings
from app.runtime import open_runtime
from app.storage.database import Database


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings if settings is not None else Settings()
    database = Database(configured.database_path)

    @asynccontextmanager
    async def lifespan(application):
        async with open_runtime(configured, database=database) as runtime:
            for key, value in vars(runtime).items():
                setattr(application.state, key, value)
            yield

    application = FastAPI(title=configured.app_name, lifespan=lifespan)
    application.state.settings = configured
    application.state.database = database
    application.include_router(health_router)
    return application
