from app.i18n import tr


def station_text(station: dict, lang: str) -> str:
    tags = station["tags"]
    name = tags.get("name") or tags.get("operator") or tr(lang, "station_name_unknown")
    address_parts = [
        " ".join(filter(None, [tags.get("addr:street"), tags.get("addr:housenumber")])),
        tags.get("addr:place"),
        tags.get("addr:city") or tags.get("addr:town") or tags.get("addr:village"),
    ]
    address = tags.get("addr:full") or ", ".join(part for part in address_parts if part)
    missing = []
    if not address:
        missing.append(tr(lang, "address_unknown"))
    elif not any(tags.get(key) for key in ("addr:full", "addr:street", "addr:place")):
        missing.append(tr(lang, "address_incomplete"))

    # OSM socket metadata belongs to its connector; it is not a connector count.
    connector_types = sorted({key.split(":")[1] for key in tags if key.startswith("socket:") and len(key.split(":")) >= 2})
    connectors = []
    for connector in connector_types:
        count = tags.get(f"socket:{connector}")
        if count in ("no", "0", 0):
            continue
        details = []
        if count and str(count).isdigit() and int(count) > 0:
            details.append(f"×{count}")
        elif count != "yes":
            details.append(tr(lang, "connector_count_unknown"))
            if tr(lang, "connector_count_unknown") not in missing:
                missing.append(tr(lang, "connector_count_unknown"))
        for field in ("output", "voltage", "current"):
            value = tags.get(f"socket:{connector}:{field}")
            if value:
                details.append(str(value))
        connectors.append(f"{connector}: {', '.join(details)}" if details else connector)
    if not connectors:
        missing.append(tr(lang, "connectors_unknown"))
    if not tags.get("name") and not tags.get("operator"):
        missing.append(tr(lang, "station_name_unknown"))
    lines = [
        f"🔌 {name} — {station['distance_m']} {tr(lang, 'distance_unit')}",
        f"📍 {address or tr(lang, 'address_unknown')}",
        f"⚡ {'; '.join(connectors) or tr(lang, 'connectors_unknown')}",
    ]
    if missing:
        lines.append(f"{tr(lang, 'incomplete')} ({'; '.join(missing)})")
    if station.get("data_stale"):
        lines.append(tr(lang, "saved_data_notice"))
    if station.get("data_limited"):
        lines.append(tr(lang, "limited_data_notice"))
    lines.append(f"https://www.openstreetmap.org/{station['osm_type']}/{station['osm_id']}")
    return "\n".join(lines)
