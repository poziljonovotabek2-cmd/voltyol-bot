import asyncio
import hashlib
import json
import time

import httpx

from app import storage
from app.config import settings

_request_lock = asyncio.Lock()
_last_request_at = 0.0


async def search(params: dict) -> list[dict]:
    """Cached, serial user-triggered searches; one request per second per app process."""
    global _last_request_at
    cache_key = "nominatim:v1:" + hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
    redis = storage.redis
    async with _request_lock:
        if redis:
            try:
                cached = await redis.get(cache_key)
                if cached:
                    return json.loads(cached)
            except Exception:
                pass
        await asyncio.sleep(max(0, 1.05 - (time.monotonic() - _last_request_at)))
        _last_request_at = time.monotonic()
        async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=4), headers={"User-Agent": "VoltYolBot/0.1"}) as client:
            response = await client.get(settings.nominatim_url, params=params)
            response.raise_for_status()
            results = response.json()
        if not isinstance(results, list):
            raise ValueError("Invalid Nominatim response")
        if redis:
            try:
                await redis.setex(cache_key, 3600, json.dumps(results))
            except Exception:
                pass
        return results
