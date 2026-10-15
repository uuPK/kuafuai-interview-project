from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter()
ROOT = Path(__file__).resolve().parent.parent / "static"
HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'self'",
}


@router.get("/", include_in_schema=False)
def index():
    return FileResponse(ROOT / "index.html", headers=HEADERS)


@router.get("/assets/app.js", include_in_schema=False)
def script():
    return FileResponse(ROOT / "app.js", media_type="text/javascript", headers=HEADERS)


@router.get("/assets/app.css", include_in_schema=False)
def styles():
    return FileResponse(ROOT / "app.css", media_type="text/css", headers=HEADERS)
