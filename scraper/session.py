"""Session and cookie management for authenticated scraping."""
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class SessionManager:
    """Manages scraping sessions — cookie persistence, rotation, etc."""

    def __init__(self, redis_url: str | None = None):
        self._sessions = {}
        self._redis = None
        if redis_url:
            try:
                import redis.asyncio as redis
                self._redis = redis.from_url(redis_url)
            except Exception as e:
                logger.warning(f"Redis unavailable: {e}")

    async def save_session(self, domain: str, cookies: list[dict]):
        """Save session cookies for reuse."""
        key = f"session:{domain}"
        data = json.dumps(cookies)
        if self._redis:
            await self._redis.setex(key, 3600, data)  # 1 hour TTL
        else:
            self._sessions[key] = data

    async def load_session(self, domain: str) -> list[dict] | None:
        """Load previously saved session cookies."""
        key = f"session:{domain}"
        if self._redis:
            data = await self._redis.get(key)
            return json.loads(data) if data else None
        data = self._sessions.get(key)
        return json.loads(data) if data else None

    async def clear_session(self, domain: str):
        key = f"session:{domain}"
        if self._redis:
            await self._redis.delete(key)
        self._sessions.pop(key, None)
