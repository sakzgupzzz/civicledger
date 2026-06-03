"""Local disk-backed TTL cache for CivicLedger.

The live data sources (SEC EDGAR, FRED, House clerk) are slow — a single
fundamentals or 13F lookup can take seconds. This cache stores JSON results on
disk so repeated loads of the dashboard or API are instant, and survive process
restarts. It is a best-effort cache: any read/write error falls back to a live
fetch rather than raising.

Usage:
    from civicledger.cache import cached
    data = await cached("fundamentals/AAPL", 21600, lambda: fetch(...))
"""

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from loguru import logger

from civicledger.config import get_settings

# Per-key locks to prevent a cache stampede (many concurrent misses all
# triggering the same expensive fetch).
_locks: dict[str, asyncio.Lock] = {}


def _cache_dir() -> Optional[Path]:
    s = get_settings()
    if not s.cache_enabled:
        return None
    try:
        d = Path(s.cache_dir).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception as e:  # noqa: BLE001
        logger.debug(f"cache dir unavailable: {e}")
        return None


def _path(directory: Path, key: str) -> Path:
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return directory / f"{digest}.json"


def _read_fresh(path: Path, ttl: int, now: float) -> Optional[Any]:
    """Return cached value if the file exists and is younger than ttl seconds."""
    try:
        if not path.exists() or (now - path.stat().st_mtime) > ttl:
            return None
        with path.open("r") as f:
            payload = json.load(f)
        return payload.get("v")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"cache read failed ({path.name}): {e}")
        return None


def _write(path: Path, key: str, value: Any) -> None:
    try:
        tmp = path.with_suffix(".tmp")
        with tmp.open("w") as f:
            json.dump({"k": key, "t": time.time(), "v": value}, f, default=str)
        tmp.replace(path)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"cache write failed ({path.name}): {e}")


async def cached(key: str, ttl: int, factory: Callable[[], Awaitable[Any]]) -> Any:
    """Return a cached value for ``key`` or compute it via ``factory`` and store it.

    Args:
        key: Stable cache key (e.g. "fundamentals/AAPL").
        ttl: Seconds the cached value stays fresh.
        factory: Async callable producing the value on a cache miss.

    Caching is skipped entirely if disabled or the cache dir is unwritable —
    the factory is awaited directly. Falsy results (None/[]/{}) are NOT cached,
    so transient empty responses don't get pinned.
    """
    directory = _cache_dir()
    if directory is None:
        return await factory()

    path = _path(directory, key)
    now = time.time()

    fresh = _read_fresh(path, ttl, now)
    if fresh is not None:
        logger.debug(f"cache hit: {key}")
        return fresh

    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        # Re-check inside the lock in case another task just populated it.
        fresh = _read_fresh(path, ttl, time.time())
        if fresh is not None:
            return fresh
        value = await factory()
        if value:  # don't cache empty/None
            _write(path, key, value)
        return value


def clear_cache() -> int:
    """Delete all cached files. Returns the number removed."""
    directory = _cache_dir()
    if directory is None:
        return 0
    n = 0
    for f in directory.glob("*.json"):
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    return n
