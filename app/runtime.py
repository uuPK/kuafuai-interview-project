"""Shared, lazy application resources for API and Worker processes."""

from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace

from app.agent.brief_rules import BriefRules
from app.agent.review import IndependentReviewer
from app.auth.service import AuthService
from app.harness.trace import configure_event_logging
from app.providers.bocha import BochaClient
from app.providers.deepseek import DeepSeekClient
from app.providers.feeds import FeedCatalog, FeedClient
from app.providers.public_http import PublicHttpClient
from app.storage.auth import AuthStore
from app.storage.briefs import BriefStore
from app.storage.calls import CallStore
from app.storage.candidates import CandidateStore
from app.storage.catalog import CatalogStore
from app.storage.conversations import ConversationStore
from app.storage.database import Database
from app.storage.materials import MaterialStore
from app.storage.outbox import OutboxStore
from app.storage.research import ResearchStore
from app.storage.runs import RunStore
from app.storage.sources import SourceStore
from app.storage.subscriptions import SubscriptionStore
from app.storage.timeline import TimelineStore
from app.tools.bash import register_bash
from app.tools.executor import ToolExecutor
from app.tools.feed import register_feed
from app.tools.fetch import register_fetch
from app.tools.files import register_file_tools
from app.tools.registry import ToolRegistry
from app.tools.research import register_research
from app.tools.search import register_search
from app.tools.submit import register_submission


@asynccontextmanager
async def open_runtime(configured, *, database=None):
    configure_event_logging()
    database = database or Database(configured.database_path)
    application = SimpleNamespace(state=SimpleNamespace(settings=configured, database=database))
    database.initialize()
    application.state.auth_store = AuthStore(database, configured)
    application.state.auth_service = AuthService(application.state.auth_store)
    application.state.run_store = RunStore(database, configured)
    subscriptions = SubscriptionStore(application.state.run_store, configured)
    application.state.subscription_store = subscriptions
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
        briefs = BriefStore(
            research,
            BriefRules(sources, configured),
            outbox=OutboxStore(
                application.state.run_store, delivery_enabled=subscriptions.delivery_enabled
            ),
        )
        application.state.brief_store = briefs
        application.state.outbox_store = briefs.outbox
        application.state.timeline_store = TimelineStore(application.state.run_store)
        application.state.catalog_store = CatalogStore(research, briefs, configured)
        reviewer = IndependentReviewer(model, research, sources, configured)
        application.state.brief_reviewer = reviewer
        register_submission(registry, briefs, reviewer=reviewer)
        application.state.tool_registry = registry
        application.state.tool_executor = ToolExecutor(
            registry, application.state.call_store, configured
        )
        yield application.state
