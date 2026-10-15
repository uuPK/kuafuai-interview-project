import asyncio

from app.harness.execution import LeaseLostError
from app.providers.smtp import SendOutcome


class Mailer:
    def __init__(self, store, transport):
        self.store, self.transport = store, transport

    async def run_once(self):
        await asyncio.to_thread(self.store.recover_expired)
        if not self.transport.ready:
            return None
        claim = await asyncio.to_thread(self.store.claim)
        if claim is None:
            return None
        record, message_id, token = claim
        try:
            result = await asyncio.to_thread(
                self.transport.send,
                record,
                message_id,
                admit=lambda: self.store.admit(record.brief_id, token),
            )
        except Exception:
            result = SendOutcome("unknown", "SMTP_RESULT_UNKNOWN")
        try:
            return await asyncio.to_thread(self.store.finish, record.brief_id, token, result)
        except LeaseLostError:
            return "unknown"
