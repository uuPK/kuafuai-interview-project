"""Signed keyset cursors bound to the owner and collection."""

import base64
import hmac
import json
import re
from datetime import datetime

from app.auth.security import mac
from app.product.models import ProductError
from app.storage.models import timestamp


class Cursors:
    def __init__(self, settings):
        self.settings = settings

    def encode(self, owner, kind, created, identifier):
        payload = base64.urlsafe_b64encode(json.dumps([created, identifier]).encode()).decode()
        return payload + "." + mac(self.settings, "page-" + kind, owner + ":" + payload)

    def decode(self, owner, kind, cursor):
        if cursor is None:
            return None
        try:
            if not isinstance(cursor, str) or len(cursor) > 512:
                raise ValueError
            payload, signature = cursor.split(".")
            if not re.fullmatch(r"[A-Za-z0-9_=-]+", payload):
                raise ValueError
            expected = mac(self.settings, "page-" + kind, owner + ":" + payload)
            if not hmac.compare_digest(expected, signature):
                raise ValueError
            values = json.loads(base64.b64decode(payload, altchars=b"-_", validate=True))
            if not isinstance(values, list) or len(values) != 2:
                raise ValueError
            created, identifier = values
            if not isinstance(created, str) or not isinstance(identifier, str):
                raise ValueError
            if (
                timestamp(datetime.fromisoformat(created)) != created
                or not 1 <= len(identifier) <= 128
            ):
                raise ValueError
            return created, identifier
        except (ValueError, TypeError, UnicodeError):
            raise ProductError("INVALID_CURSOR", 422) from None
