"""Tests for cache/redis_cache.py — Upstash Redis is mocked."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cache.redis_cache import _MemoryCache, CacheManager, TTL


# ── _MemoryCache tests ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_memory_cache_set_and_get():
    """set then get within TTL returns the stored value."""
    cache = _MemoryCache()
    await cache.set("key1", {"data": 42}, ttl_seconds=60)
    result = await cache.get("key1")
    assert result == {"data": 42}


@pytest.mark.asyncio
async def test_memory_cache_miss_returns_none():
    """get on a missing key returns None."""
    cache = _MemoryCache()
    result = await cache.get("nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_memory_cache_expired_returns_none():
    """Entry with TTL=0 is immediately expired."""
    cache = _MemoryCache()
    # Write with a past expiry by manipulating internal state
    cache._store["stale_key"] = ({"v": 1}, time.monotonic() - 1)
    result = await cache.get("stale_key")
    assert result is None


@pytest.mark.asyncio
async def test_memory_cache_delete():
    """delete removes an existing key."""
    cache = _MemoryCache()
    await cache.set("key_del", {"x": 1}, ttl_seconds=60)
    await cache.delete("key_del")
    assert await cache.get("key_del") is None


@pytest.mark.asyncio
async def test_memory_cache_delete_nonexistent_noop():
    """Deleting a key that doesn't exist doesn't raise."""
    cache = _MemoryCache()
    await cache.delete("no_such_key")  # should not raise


@pytest.mark.asyncio
async def test_memory_cache_overwrites():
    """Subsequent set overwrites previous value."""
    cache = _MemoryCache()
    await cache.set("key", {"v": 1}, ttl_seconds=60)
    await cache.set("key", {"v": 2}, ttl_seconds=60)
    assert (await cache.get("key")) == {"v": 2}


# ── CacheManager key generation ────────────────────────────────────────────────

def test_audit_key_deterministic():
    """Same URL always produces the same audit key."""
    cm = CacheManager.__new__(CacheManager)
    cm._mem = _MemoryCache()
    cm.backend = "memory"
    cm._redis = None

    k1 = cm.audit_key("https://rarerabbit.in")
    k2 = cm.audit_key("https://rarerabbit.in")
    assert k1 == k2


def test_audit_key_different_urls_differ():
    """Different URLs produce different keys."""
    cm = CacheManager.__new__(CacheManager)
    cm._mem = _MemoryCache()
    cm.backend = "memory"
    cm._redis = None

    k1 = cm.audit_key("https://rarerabbit.in")
    k2 = cm.audit_key("https://snitch.co.in")
    assert k1 != k2


def test_audit_key_format():
    """Key starts with 'audit:' prefix."""
    cm = CacheManager.__new__(CacheManager)
    cm._mem = _MemoryCache()
    cm.backend = "memory"
    cm._redis = None

    key = cm.audit_key("https://rarerabbit.in")
    assert key.startswith("audit:")


# ── TTL constants ──────────────────────────────────────────────────────────────

def test_ttl_values_are_positive():
    for name, value in TTL.items():
        assert value > 0, f"TTL for {name!r} must be positive"


def test_ttl_audit_is_24h():
    assert TTL["audit"] == 86400


def test_ttl_pagespeed_is_6h():
    assert TTL["pagespeed"] == 21600


def test_ttl_search_is_2h():
    assert TTL["search"] == 7200


# ── CacheManager integration (memory backend) ──────────────────────────────────

@pytest.mark.asyncio
async def test_cache_manager_memory_backend_get_set():
    """CacheManager using memory backend can set and get values."""
    # Force memory backend by providing no Upstash credentials
    with patch.dict("os.environ", {}, clear=False):
        # Remove any upstash env vars if present
        import os
        env_backup = {}
        for k in ("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"):
            env_backup[k] = os.environ.pop(k, None)

        try:
            cm = CacheManager()
            assert cm.backend == "memory"

            await cm.set("test:key", {"hello": "world"}, ttl_seconds=60)
            result = await cm.get("test:key")
            assert result == {"hello": "world"}
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v


@pytest.mark.asyncio
async def test_cache_manager_invalidate_removes_key():
    """invalidate removes a stored key."""
    import os
    env_backup = {}
    for k in ("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"):
        env_backup[k] = os.environ.pop(k, None)

    try:
        cm = CacheManager()
        await cm.set("inv:key", {"data": 1}, ttl_seconds=60)
        await cm.invalidate("inv:key")
        assert await cm.get("inv:key") is None
    finally:
        for k, v in env_backup.items():
            if v is not None:
                os.environ[k] = v


@pytest.mark.asyncio
async def test_cache_manager_upstash_backend_mocked():
    """CacheManager with Upstash credentials uses Redis backend."""
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value='{"cached": true}')
    mock_redis.set = AsyncMock()

    # _UPSTASH_URL/_UPSTASH_TOKEN are module-level constants (read at import
    # time). patch.dict(os.environ) is too late — patch the vars directly.
    # Redis is imported locally inside __init__, so patch at the source module.
    with (
        patch("cache.redis_cache._UPSTASH_URL", "https://fake.upstash.io"),
        patch("cache.redis_cache._UPSTASH_TOKEN", "fake-token"),
        patch("upstash_redis.asyncio.Redis", return_value=mock_redis),
    ):
        cm = CacheManager()
        assert cm.backend == "upstash"

        result = await cm.get("any:key")
        assert result == {"cached": True}
        mock_redis.get.assert_called_once_with("any:key")
