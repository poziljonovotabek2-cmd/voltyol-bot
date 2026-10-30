import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app import chargers, storage
import app.telegram as bot
from app.cards import station_text
from app.i18n import tr


USER = -7700000000102


def message(text=None, location=None):
    return {"message": {"from": {"id": USER}, "chat": {"id": USER}, "text": text, "location": location}}


def callback(data):
    return {"callback_query": {"id": "qa", "from": {"id": USER}, "message": {"chat": {"id": USER}}, "data": data}}


def station(tags):
    return {"osm_type": "node", "osm_id": 123, "lat": 41.3, "lon": 69.2, "distance_m": 123, "tags": tags}


class DialogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        bot.pending_inputs.clear()
        self.sender = AsyncMock(return_value={"ok": True})
        self.feedback = AsyncMock()
        self.report = AsyncMock()
        self.search = AsyncMock()
        self.geo = AsyncMock(return_value=(41.3, 69.2))
        self.language = AsyncMock(return_value="ru")
        for name, mock in (("telegram", self.sender), ("get_language", self.language),
                           ("save_station_feedback", self.feedback), ("save_report", self.report),
                           ("show_stations", self.search), ("geocode_city", self.geo)):
            patcher = patch.object(bot, name, mock)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(bot.pending_inputs.clear)

    async def test_help_button_and_command_in_all_languages(self):
        for lang in ("ru", "uz", "en"):
            self.language.return_value = lang
            buttons = [button["text"] for row in bot.keyboard(lang)["keyboard"] for button in row]
            self.assertIn(tr(lang, "help"), buttons)
            for text in (tr(lang, "help"), "/help", "/help@VoltYolBot"):
                bot.pending_inputs[USER] = bot.PendingInput("station_report", "node", 123)
                await bot.handle_update(message(text))
                self.assertEqual(self.sender.call_args.args[1]["text"], tr(lang, "help_text"))
                self.assertNotIn(USER, bot.pending_inputs)
                self.assertIn("/city", self.sender.call_args.args[1]["text"])
                self.feedback.assert_not_awaited()
                self.report.assert_not_awaited()
                self.geo.assert_not_awaited()

    async def test_switch_from_station_issue_to_city_in_every_language(self):
        for lang in ("ru", "uz", "en"):
            self.language.return_value = lang
            await bot.handle_update(callback("issue:node:123"))
            await bot.handle_update(message(tr(lang, "search_city")))
            await bot.handle_update(message("Tashkent"))
            self.geo.assert_awaited_with("Tashkent")
            self.search.assert_awaited_with(USER, lang, 41.3, 69.2, radius=15000)
            self.feedback.assert_not_awaited()
            self.assertNotIn(USER, bot.pending_inputs)

    async def test_start_discards_station_report_and_generic_report(self):
        for prompt in (callback("issue:node:123"), message("/report")):
            await bot.handle_update(prompt)
            await bot.handle_update(message("/start"))
            await bot.handle_update(message("Samarkand"))
            self.geo.assert_awaited_with("Samarkand")
            self.feedback.assert_not_awaited()
            self.report.assert_not_awaited()

    async def test_new_generic_report_replaces_city_prompt(self):
        await bot.handle_update(message("/city"))
        await bot.handle_update(message("/report"))
        await bot.handle_update(message("Broken socket"))
        self.report.assert_awaited_once_with(USER, "Broken socket")
        self.geo.assert_not_awaited()

    async def test_station_feedback_still_saves_correct_station(self):
        await bot.handle_update(callback("issue:way:456"))
        await bot.handle_update(message("Broken socket"))
        self.feedback.assert_awaited_with(USER, "way", 456, "problem", "Broken socket")
        await bot.handle_update(callback("confirm:way:456"))
        self.feedback.assert_awaited_with(USER, "way", 456, "working")

    async def test_location_cancels_old_station_report(self):
        await bot.handle_update(callback("issue:node:123"))
        await bot.handle_update(message(location={"latitude": 41.3, "longitude": 69.2}))
        self.assertNotIn(USER, bot.pending_inputs)
        self.search.assert_awaited_once_with(USER, "ru", 41.3, 69.2)

    async def test_location_source_outage_has_specific_message(self):
        self.search.side_effect = chargers.DataSourceUnavailable("offline")
        for lang in ("ru", "uz", "en"):
            self.language.return_value = lang
            await bot.handle_update(message(location={"latitude": 41.3, "longitude": 69.2}))
            self.assertEqual(self.sender.call_args.args[1]["text"], tr(lang, "source_unavailable"))


class CardTests(unittest.TestCase):
    def test_saved_data_warning_is_localized(self):
        for lang in ("ru", "uz", "en"):
            text = station_text({**station({}), "data_stale": True}, lang)
            self.assertIn(tr(lang, "saved_data_notice"), text)

    def test_metadata_only_connector_is_explicitly_incomplete(self):
        for lang in ("ru", "uz", "en"):
            text = station_text(station({"name": "QA", "addr:full": "Test 1", "socket:type2:output": "22 kW"}), lang)
            self.assertIn("type2", text)
            self.assertIn("22 kW", text)
            self.assertIn(tr(lang, "connector_count_unknown"), text)
            self.assertIn(tr(lang, "incomplete"), text)
            self.assertNotIn("type2:output:", text)

    def test_postcode_alone_warns_about_missing_address(self):
        for lang in ("ru", "uz", "en"):
            text = station_text(station({"name": "QA", "addr:postcode": "100000", "socket:type2": "2"}), lang)
            warning = text.split(tr(lang, "incomplete"))[1]
            self.assertIn(tr(lang, "address_unknown"), warning)

    def test_full_card_groups_connector_details_and_localizes_units(self):
        for lang in ("ru", "uz", "en"):
            text = station_text(station({"name": "QA", "addr:street": "Test", "addr:housenumber": "1", "socket:type2": "2", "socket:type2:output": "22 kW"}), lang)
            self.assertIn("Test 1", text)
            self.assertIn("type2: ×2, 22 kW", text)
            self.assertNotIn(tr(lang, "incomplete"), text)
            self.assertIn(f"123 {tr(lang, 'distance_unit')}", text)
            if lang != "ru":
                self.assertNotIn("123 м", text)


class CacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_nearby_coordinates_do_not_reuse_old_search_distance(self):
        values = {}
        async def get(key):
            return values.get(key)
        async def setex(key, ttl, value):
            values[key] = value
        redis = AsyncMock()
        redis.get.side_effect = get
        redis.setex.side_effect = setex
        record = {"type": "node", "id": 123, "lat": -85.1226, "lon": 0.1231, "tags": {}}
        source = AsyncMock(return_value=[record])
        with patch.object(storage, "redis", redis), patch.object(chargers, "nearby_stations", source):
            first = await chargers.find_chargers(-85.1234, 0.1231, 777)
            second = await chargers.find_chargers(-85.1226, 0.1231, 777)
            again = await chargers.find_chargers(-85.1226, 0.1231, 777)
        self.assertEqual(first[0]["distance_m"], 89)
        self.assertEqual(second[0]["distance_m"], 0)
        self.assertEqual(again, second)
        self.assertEqual(source.await_count, 2)

    async def test_cache_recomputes_distances_and_filters_current_radius(self):
        cached = [station({}), {**station({}), "osm_id": 456, "lat": 42.0}]
        redis = AsyncMock()
        redis.get.return_value = json.dumps(cached)
        with patch.object(storage, "redis", redis):
            results = await chargers.find_chargers(41.3, 69.2, 500)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["distance_m"], 0)


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_failover_on_http_error_and_incomplete_response(self):
        original_client = httpx.AsyncClient
        for mode in ("503", "remark"):
            seen = []
            def respond(request):
                seen.append(str(request.url))
                if len(seen) == 1:
                    return httpx.Response(503) if mode == "503" else httpx.Response(200, json={"remark": "timeout", "elements": []})
                return httpx.Response(200, json={"elements": [{"type": "node", "id": 123}]})
            transport = httpx.MockTransport(respond)
            with patch.object(chargers.httpx, "AsyncClient", lambda **kwargs: original_client(transport=transport, **kwargs)):
                result = await chargers.fetch_elements("test query")
            self.assertEqual(result[0]["id"], 123)
            self.assertEqual(len(seen), 2)
            self.assertNotEqual(seen[0], seen[1])

    async def test_outage_serves_saved_stations_with_warning_flag(self):
        source = AsyncMock(side_effect=[[], [{"type": "node", "id": 123, "lat": 41.3, "lon": 69.2, "tags": {}}]])
        with patch.object(storage, "redis", None), patch.object(chargers, "nearby_stations", source), patch.object(chargers, "fetch_elements", AsyncMock(side_effect=chargers.DataSourceUnavailable("offline"))):
            result = await chargers.find_chargers(41.3, 69.2, 500)
        self.assertTrue(result[0]["data_stale"])
        source.assert_awaited_with(41.3, 69.2, 500, include_stale=True)

    async def test_outage_without_saved_stations_is_not_empty_success(self):
        with patch.object(storage, "redis", None), patch.object(chargers.settings, "nominatim_fallback_enabled", False), patch.object(chargers, "nearby_stations", AsyncMock(return_value=[])), patch.object(chargers, "fetch_elements", AsyncMock(side_effect=chargers.DataSourceUnavailable("offline"))):
            with self.assertRaises(chargers.DataSourceUnavailable):
                await chargers.find_chargers(41.3, 69.2, 500)

    async def test_query_requests_node_coordinates_and_saves_node_and_way(self):
        elements = [{"type": "node", "id": 123, "lat": 41.3, "lon": 69.2, "tags": {}},
                    {"type": "way", "id": 456, "center": {"lat": 41.3, "lon": 69.2}, "tags": {}}]
        fetch = AsyncMock(return_value=elements)
        save = AsyncMock()
        with patch.object(storage, "redis", None), patch.object(chargers, "nearby_stations", AsyncMock(return_value=[])), patch.object(chargers, "fetch_elements", fetch), patch.object(chargers, "save_stations", save):
            results = await chargers.find_chargers(41.3, 69.2, 500)
        self.assertIn("out body center;", fetch.call_args.args[0])
        self.assertEqual(len(results), 2)
        self.assertEqual(len(save.call_args.args[0]), 2)

    async def test_outage_uses_limited_place_search_when_database_is_empty(self):
        poi = {"type": "node", "id": 123, "lat": 41.3, "lon": 69.2, "tags": {}, "source": "nominatim", "data_limited": True}
        save = AsyncMock()
        with patch.object(storage, "redis", None), patch.object(chargers, "nearby_stations", AsyncMock(return_value=[])), patch.object(chargers, "fetch_elements", AsyncMock(side_effect=chargers.DataSourceUnavailable("offline"))), patch.object(chargers, "find_poi_stations", AsyncMock(return_value=[poi])), patch.object(chargers, "save_stations", save):
            results = await chargers.find_chargers(41.3, 69.2, 500)
        self.assertEqual(results[0]["source"], "nominatim")
        self.assertTrue(results[0]["data_limited"])
        save.assert_awaited_once_with([poi])

    async def test_place_search_maps_coordinates_address_and_socket_tags(self):
        places = AsyncMock(return_value=[{"type": "charging_station", "osm_type": "way", "osm_id": 456, "lat": "41.3", "lon": "69.2", "name": "QA", "address": {"road": "Test", "house_number": "1"}, "extratags": {"socket:type2": "2"}}])
        with patch.object(chargers, "search_places", places):
            stations = await chargers.find_poi_stations(41.3, 69.2, 500)
        self.assertEqual(stations[0]["lat"], 41.3)
        self.assertEqual(stations[0]["tags"]["addr:street"], "Test")
        self.assertEqual(stations[0]["tags"]["socket:type2"], "2")
        self.assertTrue(stations[0]["data_limited"])
        self.assertEqual(places.call_args.args[0]["limit"], 10)
        self.assertEqual(places.call_args.args[0]["bounded"], 1)


if __name__ == "__main__":
    unittest.main()
