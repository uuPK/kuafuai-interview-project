"""ASGI factory with shared, lazy runtime resources."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.auth import router as auth_router
from app.api.boundary import ApiBoundary
from app.api.health import router as health_router
from app.api.product import router as product_router
from app.auth.models import AuthError
from app.config import Settings
from app.product.models import ProductError
from app.runtime import open_runtime
from app.storage.briefs import BriefNotFoundError
from app.storage.database import Database
from app.storage.runs import RunNotFoundError
from app.tools.models import ToolFailure


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
    application.add_middleware(ApiBoundary)

    @application.exception_handler(AuthError)
    async def auth_error(request, error):
        headers = {"Retry-After": str(error.retry_after)} if error.retry_after else None
        return JSONResponse(
            {"error": {"code": error.code}}, status_code=error.status, headers=headers
        )

    @application.exception_handler(RequestValidationError)
    async def invalid_input(request, error):
        return JSONResponse(
            {
                "error": {
                    "code": "INVALID_INPUT",
                    "fields": [
                        {
                            "field": str(item["loc"][-1])
                            if item["loc"]
                            and item["loc"][-1]
                            in {
                                "email",
                                "password",
                                "nickname",
                                "topics",
                                "keywords",
                                "exclude_keywords",
                                "max_items",
                                "enabled",
                                "push_time",
                                "cursor",
                                "limit",
                                "after_id",
                            }
                            else "[unknown]",
                            "code": item["type"],
                        }
                        for item in error.errors()[:10]
                    ],
                }
            },
            status_code=422,
        )

    @application.exception_handler(ProductError)
    async def product_error(request, error):
        return JSONResponse({"error": {"code": error.code}}, status_code=error.status)

    @application.exception_handler(BriefNotFoundError)
    @application.exception_handler(RunNotFoundError)
    async def not_found(request, error):
        return JSONResponse({"error": {"code": "NOT_FOUND"}}, status_code=404)

    @application.exception_handler(ToolFailure)
    async def state_problem(request, error):
        return JSONResponse({"error": {"code": error.result.error.code}}, status_code=409)

    application.include_router(health_router)
    application.include_router(auth_router)
    application.include_router(product_router)
    return application
