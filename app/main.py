"""ASGI factory: validate settings, then initialize storage during startup."""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.config import Settings
from app.providers.bocha import BochaClient
from app.providers.deepseek import DeepSeekClient
from app.providers.feeds import FeedCatalog, FeedClient
from app.providers.public_http import PublicHttpClient
from app.storage.calls import CallStore
from app.storage.candidates import CandidateStore
from app.storage.conversations import ConversationStore
from app.storage.database import Database
from app.storage.materials import MaterialStore
from app.storage.research import ResearchStore
from app.storage.runs import RunStore
from app.storage.sources import SourceStore
from app.tools.bash import register_bash
from app.tools.executor import ToolExecutor
from app.tools.feed import register_feed
from app.tools.fetch import register_fetch
from app.tools.files import register_file_tools
from app.tools.registry import ToolRegistry
from app.tools.research import register_research
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
            public_http = await stack.enter_async_context(PublicHttpClient(configured))
            feed_catalog = FeedCatalog.load(configured)
            application.state.feed_catalog = feed_catalog
            register_feed(
                registry, FeedClient(public_http, configured), candidates, configured, feed_catalog
            )
            sources = SourceStore(candidates, configured)
            application.state.public_http_client = public_http
            application.state.source_store = sources
            register_fetch(registry, public_http, sources, configured)
            materials = MaterialStore(sources, configured)
            application.state.material_store = materials
            register_file_tools(registry, materials, configured)
            register_bash(registry, materials, configured)
            research = ResearchStore(application.state.call_store, configured)
            application.state.research_store = research
            register_research(registry, research, configured)
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
