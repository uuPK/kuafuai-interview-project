"""JSON authentication endpoints and reusable server-owned identity dependency."""

from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict

from app.auth.models import AuthError, Credentials, Registration
from app.auth.security import anonymous, csrf, valid_anonymous, valid_csrf
from app.auth.service import AuthService

SESSION_COOKIE = "news_session"
BOOTSTRAP_COOKIE = "news_auth_bootstrap"
router = APIRouter(prefix="/auth")


def service(request: Request):
    auth = request.app.state.auth_service
    if auth.store.settings.session_secret is None:
        raise AuthError("AUTH_UNAVAILABLE", 503)
    return auth


def current_user(request: Request):
    auth = service(request)
    account = auth.store.authenticate(request.cookies.get(SESSION_COOKIE))
    if account is None:
        raise AuthError("AUTH_REQUIRED")
    return account


def origin(url):
    value = urlsplit(url)
    if value.scheme not in {"http", "https"} or not value.hostname or value.path not in {"", "/"}:
        raise ValueError("invalid origin")
    if value.username or value.password or value.query or value.fragment:
        raise ValueError("invalid origin")
    return value.scheme, value.hostname, value.port or (443 if value.scheme == "https" else 80)


def require_csrf(request: Request):
    auth = service(request)
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise AuthError("CSRF_REJECTED", 403)
    if request.headers.get("origin"):
        try:
            if origin(request.headers["origin"]) != origin(str(request.base_url)):
                raise ValueError("origin mismatch")
        except ValueError:
            raise AuthError("CSRF_REJECTED", 403) from None
    token = request.cookies.get(SESSION_COOKIE)
    account = auth.store.authenticate(token)
    anonymous_session = account is None
    if anonymous_session:
        token = request.cookies.get(BOOTSTRAP_COOKIE)
        if not valid_anonymous(token, auth.store.now()):
            raise AuthError("CSRF_REJECTED", 403)
    if not valid_csrf(
        auth.store.settings, token, request.headers.get("x-csrf-token"), anonymous=anonymous_session
    ):
        raise AuthError("CSRF_REJECTED", 403)
    return auth


def cookie(response, settings, name, value, age):
    response.set_cookie(
        name,
        value,
        max_age=age,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_cookie(response, settings, name):
    response.delete_cookie(
        name, path="/", httponly=True, secure=settings.session_cookie_secure, samesite="lax"
    )


@router.get("/session")
def session(request: Request, response: Response):
    auth = service(request)
    token = request.cookies.get(SESSION_COOKIE)
    account = auth.store.authenticate(token)
    if account is not None:
        return {
            "authenticated": True,
            "user": account.model_dump(),
            "csrf_token": csrf(auth.store.settings, token),
        }
    token, protection = anonymous(auth.store.settings, auth.store.now())
    cookie(response, auth.store.settings, BOOTSTRAP_COOKIE, token, 600)
    clear_cookie(response, auth.store.settings, SESSION_COOKIE)
    return {"authenticated": False, "user": None, "csrf_token": protection}


def signed_in(response, auth, result):
    account, token = result
    cookie(
        response,
        auth.store.settings,
        SESSION_COOKIE,
        token,
        auth.store.settings.session_ttl_seconds,
    )
    clear_cookie(response, auth.store.settings, BOOTSTRAP_COOKIE)
    return {
        "authenticated": True,
        "user": account.model_dump(),
        "csrf_token": csrf(auth.store.settings, token),
    }


ProtectedAuth = Annotated[AuthService, Depends(require_csrf)]


@router.post("/register", status_code=201)
async def register(form: Registration, request: Request, response: Response, auth: ProtectedAuth):
    result = await auth.register(
        form,
        request.client.host if request.client else "unknown",
        request.cookies.get(SESSION_COOKIE),
    )
    return signed_in(response, auth, result)


@router.post("/login")
async def login(form: Credentials, request: Request, response: Response, auth: ProtectedAuth):
    result = await auth.login(
        form,
        request.client.host if request.client else "unknown",
        request.cookies.get(SESSION_COOKIE),
    )
    return signed_in(response, auth, result)


class Logout(BaseModel):
    model_config = ConfigDict(extra="forbid")


@router.post("/logout")
def logout(form: Logout, request: Request, response: Response, auth: ProtectedAuth):
    current_user(request)
    auth.store.logout(request.cookies[SESSION_COOKIE])
    clear_cookie(response, auth.store.settings, SESSION_COOKIE)
    clear_cookie(response, auth.store.settings, BOOTSTRAP_COOKIE)
    return {"authenticated": False}
