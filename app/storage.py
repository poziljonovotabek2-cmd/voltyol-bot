import json
import math
from contextlib import asynccontextmanager

import asyncpg
from redis.asyncio import Redis

from app.config import settings

pool: asyncpg.Pool | None = None
redis: Redis | None = None


@asynccontextmanager
async def lifespan_resources():
    global pool, redis
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=10)
    try:
        await pool.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                language VARCHAR(2) NOT NULL DEFAULT 'ru',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE TABLE IF NOT EXISTS stations (
                osm_type TEXT NOT NULL,
                osm_id BIGINT NOT NULL,
                lat DOUBLE PRECISION NOT NULL,
                lon DOUBLE PRECISION NOT NULL,
                tags JSONB NOT NULL DEFAULT '{}'::jsonb,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (osm_type, osm_id)
            );
            CREATE INDEX IF NOT EXISTS stations_geo_idx ON stations (lat, lon);
            ALTER TABLE stations ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'overpass';
            ALTER TABLE stations ADD COLUMN IF NOT EXISTS data_limited BOOLEAN NOT NULL DEFAULT FALSE;
            CREATE TABLE IF NOT EXISTS reports (
                id BIGSERIAL PRIMARY KEY,
                telegram_id BIGINT NOT NULL,
                message TEXT NOT NULL,
                lat DOUBLE PRECISION,
                lon DOUBLE PRECISION,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE TABLE IF NOT EXISTS station_feedback (
                id BIGSERIAL PRIMARY KEY,
                telegram_id BIGINT NOT NULL,
                osm_type TEXT NOT NULL,
                osm_id BIGINT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('working', 'problem')),
                message TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
        try:
            redis = Redis.from_url(settings.redis_url, decode_responses=True)
            await redis.ping()
        except Exception:
            redis = None
        yield
    finally:
        if redis:
            await redis.aclose()
        if pool:
            await pool.close()


async def get_language(user_id: int) -> str:
    assert pool
    await pool.execute("INSERT INTO users(telegram_id) VALUES($1) ON CONFLICT(telegram_id) DO NOTHING", user_id)
    row = await pool.fetchrow("SELECT language FROM users WHERE telegram_id=$1", user_id)
    return row["language"] if row else "ru"


async def set_language(user_id: int, language: str) -> None:
    assert pool
    await pool.execute("""INSERT INTO users(telegram_id, language) VALUES($1,$2)
        ON CONFLICT(telegram_id) DO UPDATE SET language=EXCLUDED.language, updated_at=now()""", user_id, language)


async def save_stations(items: list[dict]) -> None:
    assert pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            for item in items:
                await conn.execute("""INSERT INTO stations(osm_type, osm_id, lat, lon, tags, source, data_limited)
                    VALUES($1,$2,$3,$4,$5::jsonb,$6,$7) ON CONFLICT(osm_type,osm_id) DO UPDATE
                    SET lat=EXCLUDED.lat, lon=EXCLUDED.lon,
                    tags=CASE WHEN EXCLUDED.source='nominatim' THEN stations.tags || EXCLUDED.tags ELSE EXCLUDED.tags END,
                    source=EXCLUDED.source, data_limited=EXCLUDED.data_limited, updated_at=now()""",
                    item["type"], item["id"], item["lat"], item["lon"], json.dumps(item.get("tags", {})),
                    item.get("source", "overpass"), item.get("data_limited", False))


async def nearby_stations(lat: float, lon: float, radius: int, *, include_stale: bool = False) -> list[dict]:
    assert pool
    # Bounding box in degrees; results are then ordered by approximate distance.
    lat_delta = radius / 111_000
    lon_delta = radius / (111_000 * max(0.2, abs(math.cos(math.radians(lat)))) )
    rows = await pool.fetch("""SELECT osm_type AS type, osm_id AS id, lat, lon, tags, updated_at, source, data_limited,
        (6371000 * acos(LEAST(1, GREATEST(-1, sin(radians($1))*sin(radians(lat)) +
        cos(radians($1))*cos(radians(lat))*cos(radians(lon)-radians($2)))))) AS distance
        FROM stations WHERE ($5::boolean OR updated_at > now() - interval '6 hours')
        AND lat BETWEEN $1-$3 AND $1+$3 AND lon BETWEEN $2-$4 AND $2+$4
        ORDER BY distance LIMIT 100""", lat, lon, lat_delta, lon_delta, include_stale)
    return [dict(row) for row in rows if row["distance"] <= radius]


async def save_report(user_id: int, message: str, lat: float | None = None, lon: float | None = None) -> None:
    assert pool
    await pool.execute("INSERT INTO reports(telegram_id,message,lat,lon) VALUES($1,$2,$3,$4)", user_id, message, lat, lon)


async def save_station_feedback(user_id: int, osm_type: str, osm_id: int, status: str, message: str = "") -> None:
    assert pool
    await pool.execute("""INSERT INTO station_feedback(telegram_id,osm_type,osm_id,status,message)
        VALUES($1,$2,$3,$4,$5)""", user_id, osm_type, osm_id, status, message)
