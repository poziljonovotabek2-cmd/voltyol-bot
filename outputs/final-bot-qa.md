# Final bot QA — 2026-10-30

Overall: PASS for checked bot behavior. No new functional defects found. One minor documentation inconsistency (P3) was found and corrected by the parent agent during this QA pass.

## Automated checks — PASS

Independent run: `.venv/bin/python -m unittest discover -s tests -v` — 32 tests passed in 0.427 s.

- RU/UZ/EN keyboard: location, city, language, general report and help.
- Localized help button, `/help`, `/help@VoltYolBot`: instruction text, main keyboard, unfinished input cleared.
- Search: one map and at most two separate closest distinct station recommendations; map and recommendation numbering match.
- Empty result and single station result; single station caption regression covered.
- Directions and station confirmation/issue actions; state switching and feedback station identity.
- Current distance recalculation and radius filtering on cached records.
- JPEG upload, rendered map cache, Telegram file_id reuse and invalid file_id fallback.
- Provider failover, outage handling, saved-data warning and limited Nominatim fallback.

Code review: help texts cover location/city search, map numbers, recommendations, directions, straight-line distance, feedback, language and source limitations in all three languages. README documents primary/fallback sources, partial/stale data, public provider limits, single-process restriction and caching.

## Live service checks — PASS

All checks bounded; read-only Telegram identity/configuration methods only.

| Check | Result |
| --- | --- |
| Local `/health` | HTTP 200, status ok, polling |
| Telegram `getMe` | HTTP 200, ok, bot username VoltYolBot |
| Telegram `getWebhookInfo` | HTTP 200, ok, no webhook, pending updates 0 |
| PostgreSQL `SELECT 1` | 1 |
| Redis `PING` | True |
| Saved stations within 5 km of Tashkent (41.3111, 69.2797) | 7 |

## Saved-data integration with mocked Telegram sending — PASS

Actual `show_stations` ran against configured PostgreSQL/Redis and saved Tashkent data. Only Telegram sending was replaced with AsyncMock; both external station-source entry points (`fetch_elements`, `find_poi_stations`) were patched to fail if invoked. A negative QA chat identifier was used; no user/report/feedback database records were written.

Completed in 0.779 s: one sendPhoto with 121490 photo bytes, then two separate sendMessage recommendations numbered 1 and 2. Caption reports seven stations and explicitly warns that fallback results are partial. Both recommendation keyboards contain directions, confirmation and issue actions linked to their station IDs. New external station-source calls: 0. The mock returned no file_id, so no fake Telegram photo identifier was saved.

This confirms application response construction, not actual Telegram message delivery or device display. No real sendMessage/sendPhoto/getUpdates calls were made by this QA runner. Existing running polling worker was not controlled by the runner.

## Map visual inspection — PASS

Viewed `outputs/nearby-stations-map.png` at full displayed size: readable street background, all seven numbered markers inside the viewport, nearest markers 1/2 highlighted green, blue search origin, clear legend and visible OpenStreetMap attribution. This was the saved PNG preview, not a Telegram client screenshot.

## Findings

### P3 — RESOLVED: README called outgoing map PNG while production uploads JPEG

Reproduction: read the opening search behavior paragraph, which says a successful search sends one PNG map; later the performance paragraph correctly says first uploads use JPEG and the saved preview remains PNG. `show_stations` prefers `station_map.photo` (JPEG). Documentation-only inconsistency; no user-flow failure. Parent agent corrected this wording to “one map image” after the finding. No functional change or further test run was required. The independent QA runner did not change application code.

## Scope and limits

One full automated run and one short service/integration pass. Telegram UI was not automated. No new Overpass/Nominatim station queries were made, and current public provider availability or completeness was not rechecked. Live confirmation/report writes were not exercised; those paths are covered by automated mocks. No application source changes.
