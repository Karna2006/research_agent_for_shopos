"""Caching layer — Upstash Redis when configured, in-memory TTL dict otherwise."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

# TTL constants (seconds)
TTL: dict[str, int] = {
    "audit":      86400,   # 24 h
    "pagespeed":  21600,   # 6 h
    "search":      7200,   # 2 h
    "virality":   43200,   # 12 h
}

_UPSTASH_URL   = os.getenv("UPSTASH_REDIS_URL",   "")
_UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_TOKEN", "")


class _MemoryCache:
    """Thread-safe in-process TTL cache. No external deps."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[dict, float]] = {}

    async def get(self, key: str) -> Optional[dict]:
        entry = self._store.get(key)
        if not entry:
            return None
        value, expires_at = entry
        if time.monotonic() > expires_at:
            self._store.pop(key, None)
            return None
        return value

    async def set(self, key: str, value: dict, ttl_seconds: int = 3600) -> None:
        if len(self._store) >= 500:
            # Evict oldest 100 entries by expiry time
            oldest = sorted(self._store.items(), key=lambda x: x[1][1])[:100]
            for k, _ in oldest:
                self._store.pop(k, None)
        self._store[key] = (value, time.monotonic() + ttl_seconds)

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def size(self) -> int:
        return len(self._store)


class CacheManager:
    """
    Unified cache interface.
    Uses Upstash Redis if UPSTASH_REDIS_URL + UPSTASH_REDIS_TOKEN are set,
    otherwise falls back to _MemoryCache.
    """

    def __init__(self) -> None:
        self._redis = None
        self._mem   = _MemoryCache()
        self.backend = "memory"

        if _UPSTASH_URL and _UPSTASH_TOKEN:
            try:
                from upstash_redis.asyncio import Redis  # type: ignore
                self._redis  = Redis(url=_UPSTASH_URL, token=_UPSTASH_TOKEN)
                self.backend = "upstash"
                logger.info("Cache: Upstash Redis connected (%s)", _UPSTASH_URL[:30])
            except Exception as exc:
                logger.warning("Cache: Upstash init failed — using memory. %s", exc)
        else:
            logger.info("Cache: no Upstash configured — using in-memory TTL cache")

    # ── Key builders ────────────────────────────────────────────────────────────

    @staticmethod
    def _h(text: str) -> str:
        return hashlib.sha256(text.strip().lower().encode()).hexdigest()[:16]

    def audit_key(self, url: str) -> str:
        return f"audit:{self._h(url)}"

    def virality_key(self, *parts: str) -> str:
        return f"virality:{self._h('|'.join(p for p in parts if p))}"

    def pagespeed_key(self, url: str) -> str:
        return f"pagespeed:{self._h(url)}"

    def search_key(self, query: str) -> str:
        return f"search:{self._h(query)}"

    # ── Core operations ─────────────────────────────────────────────────────────

    async def get(self, key: str) -> Optional[dict]:
        if self._redis is not None:
            try:
                raw = await self._redis.get(key)
                if raw:
                    logger.info("Cache HIT  [redis] %s", key)
                    return json.loads(raw)
                logger.info("Cache MISS [redis] %s", key)
                return None
            except Exception as exc:
                logger.warning("Cache: Redis GET failed, falling back to memory. %s", exc)

        result = await self._mem.get(key)
        logger.info("Cache %s [memory] %s", "HIT " if result else "MISS", key)
        return result

    async def set(self, key: str, value: dict, ttl_seconds: int = 3600) -> None:
        if self._redis is not None:
            try:
                await self._redis.set(key, json.dumps(value), ex=ttl_seconds)
                logger.info("Cache SET  [redis] %s ttl=%ds", key, ttl_seconds)
                return
            except Exception as exc:
                logger.warning("Cache: Redis SET failed, storing in memory. %s", exc)

        await self._mem.set(key, value, ttl_seconds)
        logger.info("Cache SET  [memory] %s ttl=%ds", key, ttl_seconds)

    async def invalidate(self, key: str) -> None:
        if self._redis is not None:
            try:
                await self._redis.delete(key)
                logger.info("Cache DEL  [redis] %s", key)
                return
            except Exception as exc:
                logger.warning("Cache: Redis DEL failed. %s", exc)

        await self._mem.delete(key)
        logger.info("Cache DEL  [memory] %s", key)
