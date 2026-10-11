"""ASGI factory: validate settings, then initialize storage during startup."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.config import Settings
from app.storage.calls import CallStore
from app.storage.conversations import ConversationStore
from app.storage.database import Database
from app.storage.runs import RunStore


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings if settings is not None else Settings()
    database = Database(configured.database_path)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        database.initialize()
        application.state.run_store = RunStore(database, configured)
        application.state.call_store = CallStore(application.state.run_store)
        application.state.conversation_store = ConversationStore(application.state.call_store)
        yield

    application = FastAPI(title=configured.app_name, lifespan=lifespan)
    application.state.settings = configured
    application.state.database = database
    application.include_router(health_router)
    return application
