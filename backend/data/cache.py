"""
StockVest — data/cache.py
In-memory LRU cache with TTL. Optionally backed by Redis if available.
"""
import time, json
from collections import OrderedDict
from typing import Any, Optional

class _MemCache:
    def __init__(self, maxsize=2000):
        self._store: OrderedDict = OrderedDict()
        self._maxsize = maxsize

    async def get(self, key: str) -> Optional[Any]:
        if key not in self._store:
            return None
        val, expiry = self._store[key]
        if expiry and time.time() > expiry:
            del self._store[key]
            return None
        self._store.move_to_end(key)
        return val

    async def set(self, key: str, value: Any, ttl: int = 300):
        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = (value, time.time() + ttl if ttl else None)
        if len(self._store) > self._maxsize:
            self._store.popitem(last=False)

    async def delete(self, key: str):
        self._store.pop(key, None)

    async def ping(self) -> str:
        return "memory"

    async def init(self):
        pass


class Cache:
    _backend: _MemCache = _MemCache()

    @classmethod
    async def init(cls):
        try:
            import redis.asyncio as aioredis
            import os
            r = aioredis.from_url(os.getenv("REDIS_URL","redis://localhost:6379"), decode_responses=True)
            await r.ping()
            cls._backend = _RedisCache(r)
            print("✓ Redis cache connected")
        except Exception:
            cls._backend = _MemCache()
            print("ℹ Using in-memory cache (Redis not available)")

    @classmethod
    async def get(cls, key: str): return await cls._backend.get(key)

    @classmethod
    async def set(cls, key: str, value, ttl=300): await cls._backend.set(key, value, ttl)

    @classmethod
    async def delete(cls, key: str): await cls._backend.delete(key)

    @classmethod
    async def ping(cls): return await cls._backend.ping()


class _RedisCache:
    def __init__(self, r): self._r = r

    async def get(self, key):
        v = await self._r.get(key)
        return json.loads(v) if v else None

    async def set(self, key, value, ttl=300):
        await self._r.setex(key, ttl, json.dumps(value, default=str))

    async def delete(self, key): await self._r.delete(key)

    async def ping(self): return "redis"
