import asyncio
import hashlib
import io
import json
import logging
import math
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont

from app.config import settings
from app.i18n import tr

logger = logging.getLogger(__name__)
WIDTH, MAP_HEIGHT, HEIGHT = 768, 468, 512
TILE_TTL = 7 * 24 * 3600


@dataclass(frozen=True)
class StationMap:
    png: bytes
    roads_available: bool
    photo: bytes | None = None


def map_fingerprint(lat: float, lon: float, stations: list[dict], lang: str) -> str:
    values = {"version": 2, "lat": lat, "lon": lon, "lang": lang, "tiles": settings.map_tile_url,
              "stations": [(item["osm_type"], item["osm_id"], item["lat"], item["lon"]) for item in stations]}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def read_render_cache(key: str) -> StationMap | None:
    root = Path(settings.map_tile_cache_dir).parent / "map-renders"
    try:
        meta = json.loads((root / f"{key}.json").read_text())
        ttl = 3600 if meta["roads_available"] else 10
        if time.time() - (root / f"{key}.json").stat().st_mtime > ttl:
            return None
        return StationMap((root / f"{key}.png").read_bytes(), meta["roads_available"], (root / f"{key}.jpg").read_bytes())
    except (OSError, ValueError, KeyError):
        return None


def save_render_cache(key: str, result: StationMap) -> None:
    root = Path(settings.map_tile_cache_dir).parent / "map-renders"
    try:
        root.mkdir(parents=True, exist_ok=True)
        for suffix, value in (("png", result.png), ("jpg", result.photo),
                              ("json", json.dumps({"roads_available": result.roads_available}).encode())):
            if value is not None:
                temporary = root / f"{key}.{uuid.uuid4().hex}.tmp"
                temporary.write_bytes(value)
                temporary.replace(root / f"{key}.{suffix}")
    except OSError:
        pass


def world_point(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    lat = max(-85.05112878, min(85.05112878, lat))
    size = 256 * 2**zoom
    return (lon + 180) / 360 * size, (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * size


def map_layout(lat: float, lon: float, stations: list[dict]):
    for zoom in range(17, 1, -1):
        world_size = 256 * 2**zoom
        origin = world_point(lat, lon, zoom)
        points = [origin]
        for station in stations:
            x, y = world_point(station["lat"], station["lon"], zoom)
            x = origin[0] + (x - origin[0] + world_size / 2) % world_size - world_size / 2
            points.append((x, y))
        xs, ys = zip(*points)
        if max(xs) - min(xs) <= WIDTH - 120 and max(ys) - min(ys) <= MAP_HEIGHT - 120:
            break
    left = (min(xs) + max(xs) - WIDTH) / 2
    top = (min(ys) + max(ys) - MAP_HEIGHT) / 2
    return zoom, left, top, [(x - left, y - top) for x, y in points]


def font(size: int):
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except OSError:
        return ImageFont.load_default(size=size)


def marker_positions(points: list[tuple[float, float]], roads_available: bool):
    """Move overlapping labels while keeping a leader line to the real station location."""
    positions = []
    offsets = [(dx, dy - 35) for dx in range(-120, 121, 40) for dy in range(-120, 121, 40)]
    offsets.sort(key=lambda offset: offset[0] ** 2 + (offset[1] + 35) ** 2)
    for x, y in points[1:]:
        for dx, dy in offsets:
            cx, cy = x + dx, y + dy
            if not (20 <= cx <= WIDTH - 20 and (60 if not roads_available else 20) <= cy <= MAP_HEIGHT - 20):
                continue
            if math.dist((cx, cy), points[0]) < 32:
                continue
            if all(math.dist((cx, cy), other) >= 39 for other in positions):
                break
        else:
            cx, cy = max(20, min(WIDTH - 20, x)), max(20, min(MAP_HEIGHT - 20, y - 35))
        positions.append((cx, cy))
    return positions


def compose_map(tiles: list[tuple[int, int, bytes | None]], left: float, top: float,
                points: list[tuple[float, float]], lang: str) -> StationMap:
    image = Image.new("RGB", (WIDTH, HEIGHT), "#e8edf0")
    roads_available = True
    for x, y, content in tiles:
        try:
            if content is None:
                raise ValueError("Tile unavailable")
            tile = Image.open(io.BytesIO(content)).convert("RGB")
            image.paste(tile, (round(x * 256 - left), round(y * 256 - top)))
        except (OSError, ValueError):
            roads_available = False
    draw = ImageDraw.Draw(image)
    if not roads_available:
        draw.rectangle((8, 8, WIDTH - 8, 39), fill="white")
        draw.text((16, 14), tr(lang, "map_tiles_missing"), font=font(13), fill="#41505a")
    labels = marker_positions(points, roads_available)
    for number, ((x, y), (cx, cy)) in enumerate(zip(points[1:], labels), 1):
        color = "#07804b" if number <= 2 else "#526474"
        draw.line((x, y, cx, cy), fill=color, width=2)
        draw.ellipse((x - 3, y - 3, x + 3, y + 3), fill=color)
    for number, (cx, cy) in reversed(list(enumerate(labels, 1))):
        color = "#07804b" if number <= 2 else "#526474"
        draw.ellipse((cx - 17, cy - 17, cx + 17, cy + 17), fill=color, outline="white", width=3)
        draw.text((cx, cy), str(number), anchor="mm", font=font(16), fill="white")
    x, y = points[0]
    draw.ellipse((x - 10, y - 10, x + 10, y + 10), fill="#1679ef", outline="white", width=3)
    draw.rectangle((0, MAP_HEIGHT, WIDTH, HEIGHT), fill="white")
    draw.ellipse((15, MAP_HEIGHT + 17, 25, MAP_HEIGHT + 27), fill="#1679ef")
    draw.text((33, MAP_HEIGHT + 15), tr(lang, "map_origin"), font=font(13), fill="#253441")
    draw.ellipse((170, MAP_HEIGHT + 17, 180, MAP_HEIGHT + 27), fill="#07804b")
    draw.text((188, MAP_HEIGHT + 15), tr(lang, "map_stations"), font=font(13), fill="#253441")
    draw.text((WIDTH - 12, MAP_HEIGHT + 22), "© OpenStreetMap contributors", anchor="rm", font=font(12), fill="#41505a")
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    photo = io.BytesIO()
    image.save(photo, format="JPEG", quality=85, optimize=True)
    return StationMap(output.getvalue(), roads_available, photo.getvalue())


async def render_station_map(lat: float, lon: float, stations: list[dict], lang: str) -> StationMap:
    key = map_fingerprint(lat, lon, stations, lang)
    cached = await asyncio.to_thread(read_render_cache, key)
    if cached:
        return cached
    zoom, left, top, points = map_layout(lat, lon, stations)
    namespace = hashlib.sha256(settings.map_tile_url.encode()).hexdigest()[:12]
    cache = Path(settings.map_tile_cache_dir) / namespace
    cache.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(3)
    async with httpx.AsyncClient(timeout=httpx.Timeout(5, connect=3), headers={"User-Agent": "VoltYolBot/0.1 (+https://t.me/VoltYolBot)"}) as client:
        async def tile(x: int, y: int):
            if not 0 <= y < 2**zoom:
                return x, y, None
            tile_x = x % 2**zoom
            path = cache / f"{zoom}-{tile_x}-{y}.png"
            try:
                if path.exists() and time.time() - path.stat().st_mtime < TILE_TTL:
                    return x, y, path.read_bytes()
                async with semaphore:
                    response = await client.get(settings.map_tile_url.format(z=zoom, x=tile_x, y=y))
                    response.raise_for_status()
                content = response.content
                # Validate before caching: HTML errors must not poison future maps.
                with Image.open(io.BytesIO(content)) as candidate:
                    candidate.verify()
                path.write_bytes(content)
                return x, y, content
            except (httpx.HTTPError, OSError, ValueError):
                return x, y, None
        tasks = [tile(x, y)
                 for x in range(math.floor(left / 256), math.floor((left + WIDTH - 1) / 256) + 1)
                 for y in range(math.floor(top / 256), math.floor((top + MAP_HEIGHT - 1) / 256) + 1)]
        tiles = await asyncio.gather(*tasks)
    result = await asyncio.to_thread(compose_map, tiles, left, top, points, lang)
    await asyncio.to_thread(save_render_cache, key, result)
    return result
