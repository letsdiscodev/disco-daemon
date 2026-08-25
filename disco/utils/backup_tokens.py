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
        # A token is good for `uses` validate() calls or ttl_seconds, whichever
        # runs out first.
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
