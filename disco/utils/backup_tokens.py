"""Short-lived bearer tokens for the internal backup-pull endpoint.

Each backup-push issues one token, passes it to the global-job tasks via
env var, and revokes it once the job completes. Tokens are held in
memory only — they survive nowhere across daemon restarts, which is the
right behavior (a fresh daemon issues fresh tokens for its own pushes).
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timedelta, timezone


class BackupTokenRegistry:
    def __init__(self) -> None:
        self._tokens: dict[str, datetime] = {}
        self._lock = asyncio.Lock()

    async def issue(self, ttl_seconds: int = 900) -> str:
        token = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        async with self._lock:
            self._tokens[token] = expires
            self._gc()
        return token

    async def validate(self, token: str) -> bool:
        async with self._lock:
            self._gc()
            expiry = self._tokens.get(token)
            return expiry is not None

    async def revoke(self, token: str) -> None:
        async with self._lock:
            self._tokens.pop(token, None)

    def _gc(self) -> None:
        now = datetime.now(timezone.utc)
        expired = [t for t, exp in self._tokens.items() if exp <= now]
        for t in expired:
            del self._tokens[t]


backup_tokens = BackupTokenRegistry()
