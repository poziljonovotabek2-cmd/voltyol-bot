import io
import math
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

import app.telegram as bot
from app.i18n import tr
from app.maps import HEIGHT, MAP_HEIGHT, WIDTH, StationMap, compose_map, map_layout, marker_positions


def station(osm_id, distance):
    return {"osm_type": "node", "osm_id": osm_id, "distance_m": distance,
            "lat": 41.31 + osm_id * 0.001, "lon": 69.28,
            "tags": {"name": f"Station {osm_id}"}}


class ResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_map_then_two_nearest_recommendations_in_all_languages(self):
        for lang in ("ru", "uz", "en"):
            sender = AsyncMock(return_value={"ok": True})
            renderer = AsyncMock(return_value=StationMap(b"image-bytes", True))
            # Unsorted input and duplicate OSM object must not change map/card numbering.
            found = [station(3, 300), station(1, 100), station(2, 200), station(1, 100)]
            with patch.object(bot, "find_chargers", AsyncMock(return_value=found)), patch.object(bot, "render_station_map", renderer), patch.object(bot, "telegram", sender):
                await bot.show_stations(123, lang, 41.31, 69.28)
            calls = sender.call_args_list
            self.assertEqual([call.args[0] for call in calls], ["sendPhoto", "sendMessage", "sendMessage"])
            self.assertEqual(calls[0].kwargs["photo"], b"image-bytes")
            self.assertIn(tr(lang, "map_caption").format(count=3), calls[0].args[1]["caption"])
            self.assertEqual([item["osm_id"] for item in renderer.call_args.args[2]], [1, 2, 3])
            for number, call in enumerate(calls[1:], 1):
                payload = call.args[1]
                self.assertTrue(payload["text"].startswith(tr(lang, "recommendation").format(number=number)))
                self.assertIn(f"Station {number}", payload["text"])
                rows = payload["reply_markup"]["inline_keyboard"]
                self.assertIn("directions?", rows[0][0]["url"])
                self.assertEqual(rows[1][0]["callback_data"], f"confirm:node:{number}")

    async def test_one_station_gets_one_map_and_one_recommendation(self):
        for lang in ("ru", "uz", "en"):
            with self.subTest(lang=lang):
                sender = AsyncMock(return_value={"ok": True})
                with patch.object(bot, "find_chargers", AsyncMock(return_value=[station(1, 100)])), patch.object(bot, "render_station_map", AsyncMock(return_value=StationMap(b"image", False))), patch.object(bot, "telegram", sender):
                    await bot.show_stations(123, lang, 41.31, 69.28)
                self.assertEqual(sender.await_count, 2)
                caption = sender.call_args_list[0].args[1]["caption"]
                self.assertIn(tr(lang, "map_caption_single"), caption)
                self.assertNotIn("2", caption)
                self.assertIn(tr(lang, "map_tiles_missing"), caption)

    async def test_empty_search_does_not_generate_map_or_recommendations(self):
        sender, renderer = AsyncMock(), AsyncMock()
        with patch.object(bot, "find_chargers", AsyncMock(return_value=[])), patch.object(bot, "render_station_map", renderer), patch.object(bot, "telegram", sender):
            await bot.show_stations(123, "ru", 41.31, 69.28)
        self.assertEqual(sender.await_count, 1)
        self.assertEqual(sender.call_args.args[1]["text"], tr("ru", "no_results"))
        renderer.assert_not_awaited()

    async def test_photo_upload_uses_multipart_and_json_keyboard(self):
        original = httpx.AsyncClient
        def respond(request):
            self.assertIn("multipart/form-data", request.headers["content-type"])
            self.assertIn(b'filename="chargers.png"', request.content)
            self.assertIn(b"image/png", request.content)
            self.assertIn(b'"resize_keyboard": true', request.content)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        with patch.object(bot.settings, "telegram_bot_token", "unit-test-token"), patch.object(bot.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs)):
            result = await bot.telegram("sendPhoto", {"chat_id": 123, "caption": "Map", "reply_markup": bot.keyboard("en")}, photo=b"png-bytes")
        self.assertTrue(result["ok"])


class MapTests(unittest.TestCase):
    def test_coincident_stations_have_separate_number_labels(self):
        points = [(300, 250)] * 11
        labels = marker_positions(points, True)
        self.assertEqual(len(set(labels)), 10)
        for index, label in enumerate(labels):
            self.assertGreaterEqual(math.dist(label, points[0]), 32)
            for other in labels[index + 1:]:
                self.assertGreaterEqual(math.dist(label, other), 39)

    def test_origin_and_stations_fit_inside_map(self):
        _, _, _, points = map_layout(41.31, 69.28, [station(1, 100), station(2, 200)])
        for x, y in points:
            self.assertGreaterEqual(x, 50)
            self.assertLessEqual(x, WIDTH - 50)
            self.assertGreaterEqual(y, 50)
            self.assertLessEqual(y, MAP_HEIGHT - 50)

    def test_unavailable_tiles_still_produce_valid_marker_map(self):
        result = compose_map([(0, 0, None)], 0, 0, [(200, 200), (300, 300)], "ru")
        self.assertFalse(result.roads_available)
        with Image.open(io.BytesIO(result.png)) as image:
            self.assertEqual(image.size, (WIDTH, HEIGHT))


if __name__ == "__main__":
    unittest.main()
