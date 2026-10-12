"""ASGI factory: validate settings, then initialize storage during startup."""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.config import Settings
from app.providers.bocha import BochaClient
from app.providers.deepseek import DeepSeekClient
from app.storage.calls import CallStore
from app.storage.candidates import CandidateStore
from app.storage.conversations import ConversationStore
from app.storage.database import Database
from app.storage.runs import RunStore
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry
from app.tools.search import register_search


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings if settings is not None else Settings()
    database = Database(configured.database_path)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        database.initialize()
        application.state.run_store = RunStore(database, configured)
        application.state.call_store = CallStore(application.state.run_store)
        application.state.conversation_store = ConversationStore(application.state.call_store)
        async with AsyncExitStack() as stack:
            model = (
                await stack.enter_async_context(DeepSeekClient(configured))
                if not configured.missing_service_fields("deepseek")
                else None
            )
            bocha = (
                await stack.enter_async_context(BochaClient(configured))
                if not configured.missing_service_fields("bocha")
                else None
            )
            application.state.model_client = model
            application.state.bocha_client = bocha
            candidates = CandidateStore(application.state.call_store)
            application.state.candidate_store = candidates
            registry = ToolRegistry()
            register_search(registry, bocha, candidates, configured)
            application.state.tool_registry = registry
            application.state.tool_executor = ToolExecutor(
                registry, application.state.call_store, configured
            )
            yield

    application = FastAPI(title=configured.app_name, lifespan=lifespan)
    application.state.settings = configured
    application.state.database = database
    application.include_router(health_router)
    return application
