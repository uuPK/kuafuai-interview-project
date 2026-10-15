"""Run bounded password work outside database transactions and the async event loop."""

from anyio import CapacityLimiter, to_thread

from app.auth.models import AuthError
from app.auth.security import Passwords


class AuthService:
    def __init__(self, store):
        self.store, self.passwords = store, Passwords()
        self.limiter = CapacityLimiter(2)

    async def register(self, form, peer, old_token):
        self.store.throttle(peer, form.email)
        if self.store.credentials(form.email):
            raise AuthError("ACCOUNT_EXISTS", 409)
        password_hash = await to_thread.run_sync(
            self.passwords.hash,
            form.password.get_secret_value(),
            limiter=self.limiter,
        )
        return self.store.register(form, password_hash, old_token=old_token)

    async def login(self, form, peer, old_token):
        self.store.throttle(peer, form.email)
        observed = self.store.credentials(form.email)
        valid, upgraded = await to_thread.run_sync(
            self.passwords.verify,
            observed["password_hash"] if observed else None,
            form.password.get_secret_value(),
            limiter=self.limiter,
        )
        if not valid or observed is None:
            raise AuthError("INVALID_CREDENTIALS")
        return self.store.login(observed, password_hash=upgraded, old_token=old_token)
