"""Bound API JSON input before parsing, and keep private responses out of caches."""

import json

from starlette.responses import JSONResponse


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def invalid_constant(value):
    raise ValueError("non-finite JSON")


class ApiBoundary:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        is_auth = path.startswith("/auth/")
        is_product = any(
            path == prefix or path.startswith(prefix + "/")
            for prefix in ("/me", "/subscription", "/runs", "/briefs")
        )
        if scope["type"] != "http" or not (is_auth or is_product):
            return await self.app(scope, receive, send)
        original_send = send

        async def protected_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"
                ] + [(b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff")]
            await original_send(message)

        send = protected_send
        if scope["method"] in {"POST", "PUT", "PATCH"}:
            headers = dict(scope["headers"])
            if (
                headers.get(b"content-type", b"").split(b";")[0].strip().lower()
                != b"application/json"
            ):
                return await JSONResponse({"error": {"code": "JSON_REQUIRED"}}, 415)(
                    scope, receive, send
                )
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                if len(body) + len(chunk) > (4096 if is_auth else 131_072):
                    return await JSONResponse({"error": {"code": "BODY_TOO_LARGE"}}, 413)(
                        scope, receive, send
                    )
                body.extend(chunk)
                if not message.get("more_body", False):
                    break
            try:
                json.loads(body, object_pairs_hook=unique, parse_constant=invalid_constant)
            except (ValueError, RecursionError):
                return await JSONResponse({"error": {"code": "INVALID_INPUT"}}, 422)(
                    scope, receive, send
                )
            original_receive = receive
            delivered = False

            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await original_receive()

            receive = replay
        await self.app(scope, receive, send)
