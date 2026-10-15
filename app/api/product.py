"""Authenticated product API; task execution belongs to the independent Worker."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request

from app.api.auth import current_user, require_csrf
from app.auth.models import User
from app.product.models import EmptyInput, ProductError, ProfileInput, SubscriptionInput

router = APIRouter(dependencies=[Depends(current_user)])
Identity = Annotated[User, Depends(current_user)]
ResourceID = Annotated[str, Path(min_length=1, max_length=128)]


def query_keys(request, allowed):
    keys = [key for key, _ in request.query_params.multi_items()]
    if len(keys) != len(set(keys)) or set(keys) - allowed:
        raise ProductError("INVALID_QUERY", 422)


def no_query(request: Request):
    query_keys(request, set())


def page(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    cursor: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
):
    query_keys(request, {"limit", "cursor"})
    return {"limit": limit, "cursor": cursor}


def event_page(
    request: Request,
    after_id: Annotated[int, Query(ge=0, le=9_223_372_036_854_775_807)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    query_keys(request, {"limit", "after_id"})
    return {"limit": limit, "after_id": after_id}


Pagination = Annotated[dict, Depends(page)]
EventPagination = Annotated[dict, Depends(event_page)]
READ = [Depends(no_query)]
WRITE = [Depends(require_csrf), Depends(no_query)]


@router.get("/me", dependencies=READ)
def me(account: Identity):
    return {"user": account.model_dump()}


@router.put("/me", dependencies=WRITE)
def update_me(form: ProfileInput, request: Request, account: Identity):
    return {"user": request.app.state.subscription_store.profile(account.id, form).model_dump()}


@router.get("/subscription", dependencies=READ)
def subscription(request: Request, account: Identity):
    return {"subscription": request.app.state.subscription_store.get(account.id)}


@router.put("/subscription", dependencies=WRITE)
def update_subscription(form: SubscriptionInput, request: Request, account: Identity):
    return {"subscription": request.app.state.subscription_store.save(account.id, form)}


@router.post("/runs", status_code=202, dependencies=WRITE)
def preview(form: EmptyInput, request: Request, account: Identity):
    run_id, reused = request.app.state.subscription_store.preview(account.id)
    return {"run_id": run_id, "reused": reused}


@router.get("/runs")
def runs(request: Request, account: Identity, paging: Pagination):
    return request.app.state.catalog_store.runs_page(account.id, **paging)


@router.get("/runs/{run_id}", dependencies=READ)
def run(run_id: ResourceID, request: Request, account: Identity):
    return request.app.state.catalog_store.run(run_id, account.id)


@router.get("/runs/{run_id}/events")
def events(run_id: ResourceID, request: Request, account: Identity, paging: EventPagination):
    return request.app.state.timeline_store.page(run_id, account.id, **paging)


@router.get("/briefs")
def briefs(request: Request, account: Identity, paging: Pagination):
    return request.app.state.catalog_store.briefs_page(account.id, **paging)


@router.get("/briefs/{brief_id}", dependencies=READ)
def brief(brief_id: ResourceID, request: Request, account: Identity):
    return request.app.state.catalog_store.brief(brief_id, account.id)
