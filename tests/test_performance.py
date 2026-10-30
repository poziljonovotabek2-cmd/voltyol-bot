import asyncio
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

from app import chargers, storage
from app.maps import StationMap, compose_map, map_fingerprint, render_station_map, save_render_cache
import app.telegram as bot


def station():
    return {"osm_type": "node", "osm_id": 1, "lat": 41.3, "lon": 69.2, "distance_m": 0, "tags": {}}


class PerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_render_cache_skips_tile_network_and_drawing(self):
        with tempfile.TemporaryDirectory(dir="work") as temporary, patch.object(bot.settings, "map_tile_cache_dir", str(Path(temporary) / "tiles")):
            result = compose_map([(0, 0, None)], 0, 0, [(200, 200), (250, 250)], "ru")
            save_render_cache(map_fingerprint(41.3, 69.2, [station()], "ru"), result)
            with patch("app.maps.httpx.AsyncClient") as client, patch("app.maps.compose_map") as draw:
                cached = await render_station_map(41.3, 69.2, [station()], "ru")
            client.assert_not_called()
            draw.assert_not_called()
            self.assertEqual(cached.png, result.png)
            self.assertEqual(cached.photo, result.photo)

    async def test_telegram_file_id_skips_render_and_upload(self):
        redis = AsyncMock()
        redis.get.return_value = json.dumps({"file_id": "test-file-id", "roads_available": True})
        sender = AsyncMock(return_value={"ok": True})
        render = AsyncMock()
        with patch.object(storage, "redis", redis), patch.object(bot, "find_chargers", AsyncMock(return_value=[station()])), patch.object(bot, "telegram", sender), patch.object(bot, "render_station_map", render):
            await bot.show_stations(123, "ru", 41.3, 69.2)
        render.assert_not_awaited()
        self.assertEqual(sender.call_args_list[0].args[1]["photo"], "test-file-id")
        self.assertNotIn("photo", sender.call_args_list[0].kwargs)
        self.assertEqual(sender.await_count, 2)

    async def test_expired_telegram_file_id_is_replaced_with_upload(self):
        redis = AsyncMock()
        redis.get.return_value = json.dumps({"file_id": "expired", "roads_available": True})
        sender = AsyncMock(side_effect=[bot.TelegramAPIError("sendPhoto", 400),
            {"ok": True, "result": {"photo": [{"file_id": "replacement"}]}}, {"ok": True}])
        render = AsyncMock(return_value=StationMap(b"png", True, b"jpeg"))
        with patch.object(storage, "redis", redis), patch.object(bot, "find_chargers", AsyncMock(return_value=[station()])), patch.object(bot, "telegram", sender), patch.object(bot, "render_station_map", render):
            await bot.show_stations(123, "ru", 41.3, 69.2)
        self.assertEqual(sender.call_args_list[1].kwargs["photo"], b"jpeg")
        self.assertIn("replacement", redis.setex.call_args.args[2])

    async def test_outage_budget_and_cooldown_skip_repeated_wait(self):
        async def unavailable(query):
            await asyncio.sleep(10)
        slow = AsyncMock(side_effect=unavailable)
        previous = chargers._overpass_retry_after
        chargers._overpass_retry_after = 0
        try:
            with patch.object(chargers.settings, "overpass_budget_seconds", 0.03), patch.object(chargers, "_fetch_elements_once", slow):
                started = time.perf_counter()
                for _ in range(2):
                    with self.assertRaises(chargers.DataSourceUnavailable):
                        await chargers.fetch_elements("test")
                self.assertLess(time.perf_counter() - started, 0.5)
            self.assertEqual(slow.await_count, 1)
        finally:
            chargers._overpass_retry_after = previous

    async def test_outgoing_requests_share_a_client_and_close_it(self):
        original = httpx.AsyncClient
        clients = []
        def create(**kwargs):
            client = original(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True, "result": {}})), **kwargs)
            clients.append(client)
            return client
        with patch.object(bot.settings, "telegram_bot_token", "unit-test-token"), patch.object(bot.httpx, "AsyncClient", create):
            async with bot.telegram_session():
                await bot.telegram("getMe", {})
                await bot.telegram("getMe", {})
            self.assertEqual(len(clients), 1)
            self.assertTrue(clients[0].is_closed)


class PhotoTests(unittest.TestCase):
    def test_photo_is_valid_jpeg_with_same_dimensions_as_preview(self):
        result = compose_map([(0, 0, None)], 0, 0, [(200, 200), (250, 250)], "ru")
        with Image.open(io.BytesIO(result.png)) as png, Image.open(io.BytesIO(result.photo)) as jpeg:
            self.assertEqual(jpeg.format, "JPEG")
            self.assertEqual(jpeg.size, png.size)


if __name__ == "__main__":
    unittest.main()
