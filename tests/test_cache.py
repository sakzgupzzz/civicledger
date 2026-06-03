"""Offline tests for the local disk cache."""

import pytest

from civicledger import cache
from civicledger.config import get_settings


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "cache_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(s, "cache_enabled", True, raising=False)
    cache._locks.clear()
    return tmp_path


@pytest.mark.asyncio
async def test_caches_and_reuses(tmp_cache):
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        return {"v": calls["n"]}

    a = await cache.cached("k1", 60, factory)
    b = await cache.cached("k1", 60, factory)
    assert a == {"v": 1}
    assert b == {"v": 1}          # served from cache
    assert calls["n"] == 1        # factory ran once


@pytest.mark.asyncio
async def test_ttl_expiry_refetches(tmp_cache):
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        return calls["n"]

    await cache.cached("k2", 60, factory)
    await cache.cached("k2", 0, factory)   # ttl=0 -> always stale
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_does_not_cache_falsy(tmp_cache):
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        return []                          # empty -> not cached

    await cache.cached("k3", 60, factory)
    await cache.cached("k3", 60, factory)
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_disabled_bypasses(tmp_path, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "cache_enabled", False, raising=False)
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        return "x"

    await cache.cached("k4", 60, factory)
    await cache.cached("k4", 60, factory)
    assert calls["n"] == 2                  # no caching when disabled


@pytest.mark.asyncio
async def test_clear_cache(tmp_cache):
    await cache.cached("k5", 60, lambda: _const("a"))
    assert any(tmp_cache.glob("*.json"))
    removed = cache.clear_cache()
    assert removed >= 1
    assert not any(tmp_cache.glob("*.json"))


async def _const(v):
    return v
