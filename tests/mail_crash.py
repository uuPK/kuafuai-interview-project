"""Dedicated process crash immediately after actual local SMTP acceptance."""

import asyncio
import os
import sys
from datetime import datetime

from app.providers.smtp import SMTPTransport
from app.runtime import open_runtime
from tests.smtp_support import smtp_settings


async def main():
    directory, port, at = sys.argv[1:]
    config = smtp_settings(directory, int(port))
    async with open_runtime(config) as runtime:
        runtime.run_store.clock = lambda: datetime.fromisoformat(at)
        record, message_id, token = runtime.dispatch_store.claim()
        result = SMTPTransport(config).send(
            record, message_id, admit=lambda: runtime.dispatch_store.admit(record.brief_id, token)
        )
        if result.status == "sent":
            os._exit(73)
        raise AssertionError(result)


if __name__ == "__main__":
    asyncio.run(main())
