"""Respect Retry-After seconds/HTTP-date while bounding backoff by the run deadline."""

import math
from datetime import datetime
from email.utils import parsedate_to_datetime

from app.storage.models import as_utc


def retry_after_seconds(header: str | None, now: datetime) -> float | None:
    if header is None:
        return None
    value = header.strip()
    if value.isascii() and value.isdigit():
        seconds = float(value)
        return seconds if math.isfinite(seconds) else None
    try:
        date = parsedate_to_datetime(value)
        return max(0, (as_utc(date) - as_utc(now)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def retry_delay(base: float, attempt: int, header: str | None, now: datetime) -> float:
    backoff = base * (2 ** (attempt - 1))
    return max(backoff, retry_after_seconds(header, now) or 0)
