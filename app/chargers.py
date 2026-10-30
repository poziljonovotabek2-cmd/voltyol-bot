import json
import asyncio
import logging
import math
from time import perf_counter

import httpx

from app.config import settings
from app import storage
from app.nominatim import search as search_places
from app.storage import nearby_stations, save_stations

logger = logging.getLogger(__name__)
_overpass_retry_after = 0.0


class DataSourceUnavailable(RuntimeError):
    pass


async def geocode_city(city: str) -> tuple[float, float] | None:
    matches = await search_places({"q": city, "format": "jsonv2", "limit": 1})
    if not matches:
        return None
    return float(matches[0]["lat"]), float(matches[0]["lon"])


async def find_poi_stations(lat: float, lon: float, radius: int) -> list[dict]:
    # One bounded interactive search, never a grid, pagination or bulk extraction.
    extent = min(radius, 15000)
    dy = extent / 111_000
    dx = extent / (111_000 * max(0.2, abs(math.cos(math.radians(lat)))))
    matches = await search_places({
        "q": "[charging_station]", "format": "jsonv2", "bounded": 1,
        "viewbox": f"{lon-dx:.6f},{lat+dy:.6f},{lon+dx:.6f},{lat-dy:.6f}",
        "limit": 10, "addressdetails": 1, "extratags": 1,
    })
    stations = []
    for match in matches:
        if match.get("type") != "charging_station" or match.get("osm_type") not in ("node", "way", "relation"):
            continue
        tags = {**(match.get("extratags") or {}), "amenity": "charging_station"}
        if match.get("name"):
            tags["name"] = match["name"]
        address = match.get("address") or {}
        for field, target in (("road", "street"), ("house_number", "housenumber"), ("city", "city"), ("town", "town"), ("village", "village"), ("postcode", "postcode")):
            if address.get(field):
                tags[f"addr:{target}"] = address[field]
        stations.append({"type": match["osm_type"], "id": int(match["osm_id"]),
                         "lat": float(match["lat"]), "lon": float(match["lon"]),
                         "tags": tags, "source": "nominatim", "data_limited": True})
    return stations


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    a = min(1.0, max(0.0, a))
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def search_cache_key(lat: float, lon: float, radius: int) -> str:
    # Exact coordinates avoid using a cached search for a different area.
    return f"chargers:v2:{lat!r}:{lon!r}:{radius}"


def current_results(records: list[dict], lat: float, lon: float, radius: int) -> list[dict]:
    results = [station_dict(record, lat, lon) for record in records
               if distance_m(lat, lon, record["lat"], record["lon"]) <= radius]
    return sorted(results, key=lambda item: item["distance_m"])


async def fetch_elements(query: str) -> list[dict]:
    global _overpass_retry_after
    if perf_counter() < _overpass_retry_after:
        raise DataSourceUnavailable("Overpass temporarily skipped after recent failure")
    try:
        async with asyncio.timeout(settings.overpass_budget_seconds):
            return await _fetch_elements_once(query)
    except (TimeoutError, DataSourceUnavailable):
        _overpass_retry_after = perf_counter() + settings.overpass_cooldown_seconds
        raise DataSourceUnavailable("Overpass request budget exceeded or providers unavailable") from None


async def _fetch_elements_once(query: str) -> list[dict]:
    urls = list(dict.fromkeys(filter(None, [settings.overpass_url, settings.overpass_fallback_url])))
    async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=4), headers={"User-Agent": "VoltYolBot/0.1"}) as client:
        for url in urls:
            try:
                response = await client.post(url, data={"data": query})
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict) or data.get("remark") or not isinstance(data.get("elements"), list):
                    raise ValueError("Invalid or incomplete Overpass response")
                return data["elements"]
            except (httpx.HTTPError, ValueError) as exc:
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                logger.warning("Overpass provider unavailable: %s (%s, HTTP %s)", url, type(exc).__name__, status)
    raise DataSourceUnavailable("All Overpass providers are unavailable")


async def find_chargers(lat: float, lon: float, radius: int | None = None) -> list[dict]:
    redis = storage.redis
    radius = radius or settings.search_radius_meters
    cache_key = search_cache_key(lat, lon, radius)
    if redis:
        try:
            cached = await redis.get(cache_key)
            if cached:
                return current_results(json.loads(cached), lat, lon, radius)
        except Exception:
            pass

    # Use the database first so recently fetched data remains useful during Overpass outages.
    records = await nearby_stations(lat, lon, radius)
    if records:
        results = current_results(records, lat, lon, radius)
        if redis:
            try:
                await redis.setex(cache_key, settings.cache_ttl_seconds, json.dumps(results))
            except Exception:
                pass
        return results

    query_timeout = max(1, int(settings.overpass_budget_seconds) - 1)
    query = f"""[out:json][timeout:{query_timeout}];
    (node[\"amenity\"=\"charging_station\"](around:{radius},{lat},{lon});
     way[\"amenity\"=\"charging_station\"](around:{radius},{lat},{lon});
     relation[\"amenity\"=\"charging_station\"](around:{radius},{lat},{lon}););
    out body center;"""
    try:
        raw = await fetch_elements(query)
    except DataSourceUnavailable:
        saved = await nearby_stations(lat, lon, radius, include_stale=True)
        if saved:
            results = current_results([{**row, "data_stale": True} for row in saved], lat, lon, radius)
        elif settings.nominatim_fallback_enabled:
            try:
                poi_stations = await find_poi_stations(lat, lon, radius)
                await save_stations(poi_stations)
                results = current_results(poi_stations, lat, lon, radius)
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("Place-search fallback unavailable (%s)", type(exc).__name__)
                raise DataSourceUnavailable("No live or saved station data available") from None
        else:
            raise
        if redis:
            try:
                await redis.setex(cache_key, min(60, settings.cache_ttl_seconds), json.dumps(results))
            except Exception:
                pass
        return results

    normalized = []
    for item in raw:
        pos = item if item.get("type") == "node" else item.get("center", {})
        if "lat" not in pos or "lon" not in pos:
            continue
        normalized.append({"type": item["type"], "id": item["id"], "lat": pos["lat"], "lon": pos["lon"], "tags": item.get("tags", {})})
    await save_stations(normalized)
    results = current_results(normalized, lat, lon, radius)
    if redis:
        try:
            await redis.setex(cache_key, settings.cache_ttl_seconds, json.dumps(results))
        except Exception:
            pass
    return results


def station_dict(record: dict, lat: float, lon: float) -> dict:
    tags = record["tags"]
    if isinstance(tags, str):
        tags = json.loads(tags)
    return {
        "osm_type": record.get("type", record.get("osm_type")), "osm_id": record.get("id", record.get("osm_id")),
        "lat": record["lat"], "lon": record["lon"], "tags": tags,
        "distance_m": round(distance_m(lat, lon, record["lat"], record["lon"])),
        "data_stale": record.get("data_stale", False),
        "source": record.get("source", "overpass"),
        "data_limited": record.get("data_limited", False),
    }
