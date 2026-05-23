"""Short-lived bearer tokens for the internal backup-pull endpoint.

Each backup-push issues one token, passes it to the global-job tasks via
env var, and revokes it once the job completes. Tokens are held in
memory only — they survive nowhere across daemon restarts, which is the
right behavior (a fresh daemon issues fresh tokens for its own pushes).

Tokens are use-counted: issuing a token for N expected pulls (typically
one per manager) means the token becomes invalid after N validate()
calls. Combined with a short TTL, this bounds the damage a token leak
(via `docker inspect` of the service spec) could do.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass
class _TokenState:
    expires: datetime
    remaining_uses: int


class BackupTokenRegistry:
    def __init__(self) -> None:
        self._tokens: dict[str, _TokenState] = {}
        self._lock = asyncio.Lock()

    async def issue(self, *, uses: int, ttl_seconds: int = 120) -> str:
        """Issue a token valid for `uses` calls and at most ttl_seconds.

        Callers should pass uses=<expected pull count> (typically the
        number of managers participating in the global-job) so a leaked
        token can be replayed at most that many times in the worst case.
        """
        if uses < 1:
            raise ValueError("uses must be >= 1")
        token = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        async with self._lock:
            self._tokens[token] = _TokenState(expires=expires, remaining_uses=uses)
            self._gc()
        return token

    async def validate(self, token: str) -> bool:
        async with self._lock:
            self._gc()
            state = self._tokens.get(token)
            if state is None:
                return False
            state.remaining_uses -= 1
            if state.remaining_uses <= 0:
                del self._tokens[token]
            return True

    async def revoke(self, token: str) -> None:
        async with self._lock:
            self._tokens.pop(token, None)
            self._gc()

    def _gc(self) -> None:
        now = datetime.now(timezone.utc)
        expired = [t for t, s in self._tokens.items() if s.expires <= now]
        for t in expired:
            del self._tokens[t]


backup_tokens = BackupTokenRegistry()
