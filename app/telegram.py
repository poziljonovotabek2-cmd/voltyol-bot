from dataclasses import dataclass
from contextlib import asynccontextmanager
import hashlib
import json
import logging
from time import perf_counter
from typing import Literal
from urllib.parse import urlencode

import httpx

from app import storage
from app.cards import station_text
from app.chargers import DataSourceUnavailable, find_chargers, geocode_city
from app.config import settings
from app.i18n import tr
from app.maps import map_fingerprint, render_station_map
from app.metrics import measured, record
from app.storage import get_language, save_report, save_station_feedback, set_language


@dataclass(frozen=True)
class PendingInput:
    action: Literal["city", "report", "station_report"]
    osm_type: str | None = None
    osm_id: int | None = None


pending_inputs: dict[int, PendingInput] = {}
LANGUAGE_BUTTONS = {"🇷🇺 Русский": "ru", "🇺🇿 Oʻzbekcha": "uz", "🇬🇧 English": "en"}
logger = logging.getLogger(__name__)
_telegram_client: httpx.AsyncClient | None = None


class TelegramAPIError(RuntimeError):
    def __init__(self, method: str, status_code: int):
        self.status_code = status_code
        super().__init__(f"Telegram {method}: HTTP {status_code}")


@asynccontextmanager
async def telegram_session():
    global _telegram_client
    previous = _telegram_client
    client = httpx.AsyncClient(timeout=httpx.Timeout(20, connect=5), limits=httpx.Limits(
        max_connections=20, max_keepalive_connections=10, keepalive_expiry=120))
    _telegram_client = client
    try:
        yield client
    finally:
        _telegram_client = previous
        await client.aclose()


@asynccontextmanager
async def telegram_http_client():
    if _telegram_client is not None and not _telegram_client.is_closed:
        yield _telegram_client
    else:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=5)) as client:
            yield client


async def telegram(method: str, payload: dict, *, photo: bytes | None = None) -> dict:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    started = perf_counter()
    success = False
    try:
        async with telegram_http_client() as client:
            url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"
            if photo is None:
                response = await client.post(url, json=payload)
            else:
                form = {key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
                        for key, value in payload.items() if value is not None}
                jpeg = photo.startswith(b"\xff\xd8")
                filename, mime = ("chargers.jpg", "image/jpeg") if jpeg else ("chargers.png", "image/png")
                response = await client.post(url, data=form, files={"photo": (filename, photo, mime)})
        if response.is_error:
            raise TelegramAPIError(method, response.status_code)
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(f"Telegram {method}: request rejected")
        success = True
        return result
    except httpx.HTTPError:
        raise RuntimeError(f"Telegram {method}: connection failed") from None
    finally:
        record("telegram_api", method=method, seconds=round(perf_counter() - started, 4),
               upload_bytes=len(photo) if photo else 0, success=success)


def keyboard(lang: str) -> dict:
    return {"keyboard": [
        [{"text": tr(lang, "nearby"), "request_location": True}],
        [{"text": tr(lang, "search_city")}],
        [{"text": tr(lang, "language")}, {"text": tr(lang, "report")}],
        [{"text": tr(lang, "help")}],
    ], "resize_keyboard": True, "is_persistent": True}


async def send(chat_id: int, lang: str, text: str, **extra) -> None:
    await telegram("sendMessage", {"chat_id": chat_id, "text": text, "reply_markup": keyboard(lang), **extra})


async def search_error(chat_id: int, lang: str, exc: Exception) -> None:
    logger.warning("Station search failed (%s)", type(exc).__name__)
    await send(chat_id, lang, tr(lang, "source_unavailable" if isinstance(exc, DataSourceUnavailable) else "error"))


async def show_stations(chat_id: int, lang: str, lat: float, lon: float, radius: int | None = None) -> None:
    started = perf_counter()
    stations = await measured("station_search", find_chargers(lat, lon, radius))
    if not stations:
        await send(chat_id, lang, tr(lang, "no_results"))
        return
    # Keep map numbers and recommendation order consistent even if a provider is unsorted.
    nearby = []
    seen = set()
    for station in sorted(stations, key=lambda item: item["distance_m"]):
        key = (station["osm_type"], station["osm_id"])
        if key not in seen:
            seen.add(key)
            nearby.append(station)
        if len(nearby) == 10:
            break
    namespace = hashlib.sha256(settings.telegram_bot_token.encode()).hexdigest()[:12]
    photo_key = f"telegram-map:{namespace}:{map_fingerprint(lat, lon, nearby, lang)}"
    photo_reference = None
    if storage.redis:
        try:
            cached = await storage.redis.get(photo_key)
            if cached:
                data = json.loads(cached)
                if isinstance(data.get("file_id"), str) and isinstance(data.get("roads_available"), bool):
                    photo_reference = data
        except Exception:
            pass

    def caption(roads_available: bool) -> str:
        key = "map_caption_single" if len(nearby) == 1 else "map_caption"
        text = tr(lang, key).format(count=len(nearby))
        if not roads_available:
            text += "\n" + tr(lang, "map_tiles_missing")
        if any(item.get("data_limited") for item in nearby):
            text += "\n" + tr(lang, "limited_data_notice")
        return text

    photo_result = None
    if photo_reference:
        try:
            photo_result = await telegram("sendPhoto", {"chat_id": chat_id, "photo": photo_reference["file_id"],
                "caption": caption(photo_reference["roads_available"]), "reply_markup": keyboard(lang)})
        except TelegramAPIError as exc:
            if exc.status_code != 400:
                raise
            photo_reference = None
    if photo_result is None:
        station_map = await measured("map_render", render_station_map(lat, lon, nearby, lang))
        photo_result = await telegram("sendPhoto", {"chat_id": chat_id, "caption": caption(station_map.roads_available),
            "reply_markup": keyboard(lang)}, photo=station_map.photo or station_map.png)
        variants = photo_result.get("result", {}).get("photo", [])
        if variants and storage.redis:
            try:
                await storage.redis.setex(photo_key, 3600 if station_map.roads_available else 10,
                    json.dumps({"file_id": variants[-1]["file_id"], "roads_available": station_map.roads_available}))
            except Exception:
                pass
    first_message_s = perf_counter() - started
    for number, station in enumerate(nearby[:2], 1):
        route = "https://www.openstreetmap.org/directions?" + urlencode({
            "engine": "fossgis_osrm_car", "route": f"{lat},{lon};{station['lat']},{station['lon']}",
        })
        await telegram("sendMessage", {
            "chat_id": chat_id, "text": tr(lang, "recommendation").format(number=number) + "\n" + station_text(station, lang),
            "reply_markup": {"inline_keyboard": [[{"text": tr(lang, "route"), "url": route}], [
                {"text": tr(lang, "confirm"), "callback_data": f"confirm:{station['osm_type']}:{station['osm_id']}"},
                {"text": tr(lang, "station_report"), "callback_data": f"issue:{station['osm_type']}:{station['osm_id']}"},
            ]]},
            "disable_web_page_preview": True,
        })
    record("search_response", first_message_s=round(first_message_s, 4),
           total_s=round(perf_counter() - started, 4), station_count=len(nearby),
           photo_reference_reused=photo_reference is not None)


async def handle_update(update: dict) -> None:
    callback = update.get("callback_query")
    if callback:
        user_id = callback["from"]["id"]
        chat_id = callback["message"]["chat"]["id"]
        lang = await get_language(user_id)
        action, osm_type, osm_id_text = callback["data"].split(":", 2)
        osm_id = int(osm_id_text)
        await telegram("answerCallbackQuery", {"callback_query_id": callback["id"]})
        if action == "confirm":
            pending_inputs.pop(user_id, None)
            await save_station_feedback(user_id, osm_type, osm_id, "working")
            await send(chat_id, lang, tr(lang, "confirmed"))
        elif action == "issue":
            pending_inputs[user_id] = PendingInput("station_report", osm_type, osm_id)
            await send(chat_id, lang, tr(lang, "feedback_prompt"))
        return

    message = update.get("message")
    if not message:
        return
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    text = (message.get("text") or "").strip()
    lang = await get_language(user_id)

    command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text else ""
    if text == tr(lang, "help") or command == "/help":
        pending_inputs.pop(user_id, None)
        await send(chat_id, lang, tr(lang, "help_text"))
        return
    if text.startswith("/start"):
        pending_inputs.pop(user_id, None)
        await send(chat_id, lang, tr(lang, "welcome"))
        return
    if text in LANGUAGE_BUTTONS:
        pending_inputs.pop(user_id, None)
        lang = LANGUAGE_BUTTONS[text]
        await set_language(user_id, lang)
        await send(chat_id, lang, tr(lang, "welcome"))
        return
    if text == tr(lang, "language") or text == "/language":
        pending_inputs.pop(user_id, None)
        await send(chat_id, lang, tr(lang, "choose_language"), reply_markup={"keyboard": [[{"text": label}] for label in LANGUAGE_BUTTONS], "resize_keyboard": True})
        return
    if text == tr(lang, "report") or text == "/report":
        pending_inputs[user_id] = PendingInput("report")
        await send(chat_id, lang, tr(lang, "report_prompt"))
        return
    if text == tr(lang, "search_city") or text == "/city":
        pending_inputs[user_id] = PendingInput("city")
        await send(chat_id, lang, tr(lang, "city_prompt"))
        return
    state = pending_inputs.get(user_id)
    if state and state.action == "station_report" and text:
        assert state.osm_type is not None and state.osm_id is not None
        await save_station_feedback(user_id, state.osm_type, state.osm_id, "problem", text)
        pending_inputs.pop(user_id, None)
        await send(chat_id, lang, tr(lang, "feedback_saved"))
        return
    if state and state.action == "city" and text:
        pending_inputs.pop(user_id, None)
        city = text.removeprefix("/city").strip()
        try:
            coordinates = await geocode_city(city)
            if not coordinates:
                await send(chat_id, lang, tr(lang, "city_not_found"))
                return
            await show_stations(chat_id, lang, *coordinates, radius=15000)
        except Exception as exc:
            await search_error(chat_id, lang, exc)
        return
    if state and state.action == "report" and text:
        await save_report(user_id, text)
        pending_inputs.pop(user_id, None)
        await send(chat_id, lang, tr(lang, "report_saved"))
        return
    location = message.get("location")
    if location:
        pending_inputs.pop(user_id, None)
        try:
            await show_stations(chat_id, lang, location["latitude"], location["longitude"])
        except Exception as exc:
            await search_error(chat_id, lang, exc)
        return
    if text:
        try:
            coordinates = await geocode_city(text)
            if coordinates:
                await show_stations(chat_id, lang, *coordinates, radius=15000)
            else:
                await send(chat_id, lang, tr(lang, "unknown"))
        except Exception as exc:
            await search_error(chat_id, lang, exc)
