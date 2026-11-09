"""
UplandScope — Collection Map

Given a collection and a user, plots properties in that collection's
geographic scope: green = you own it, orange = for sale, red = not listed,
gray = the local file has no sale status.

The map does not scan a city. It uses the neighborhood property file when
one exists, including files whose sale status is still Unknown, and fills
in for-sale rows from the local listings snapshot. When that file is
missing, it plots properties you own that match the requirement plus
snapshot listings. Coordinates still come from the public per-property
endpoint, capped at 200.
"""

from __future__ import annotations

import concurrent.futures
import html
import json
import sys
import time
import urllib.request
from pathlib import Path

import folium

from collection_optimizer import (
    find_eligible_properties,
    load_collections,
    parse_collection_requirement,
    _lookup_property_cache_db,
)
from forsale_finder import SUPPORTABLE_TYPES, _load_neighborhood_cache

SCRIPT_DIR = Path(__file__).resolve().parent
MAPS_DIR = SCRIPT_DIR / "maps"
COORD_CACHE_PATH = SCRIPT_DIR / "cache" / "collection_map" / "coord_cache.json"
COORD_CACHE_TTL = 30 * 86400  # coordinates never change
MAX_PLOTTED = 200  # cap per-property coordinate lookups

LOCAL_NOTE = (
    "Shows properties you own in this collection and for-sale rows in the local snapshot. "
    "Unlisted properties are left out."
)
UNKNOWN_NOTE = "Gray pins have no sale status in the local neighborhood file."


def _load_coord_cache() -> dict:
    if COORD_CACHE_PATH.exists():
        try:
            return json.loads(COORD_CACHE_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save_coord_cache(cache: dict) -> None:
    COORD_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    COORD_CACHE_PATH.write_text(json.dumps(cache))


def _fetch_coords(prop_ids: list) -> dict:
    """Return {prop_id: (lat, lng)|None} via the live per-property API endpoint."""
    cache = _load_coord_cache()
    now = time.time()
    result = {}
    need = []
    for pid in prop_ids:
        entry = cache.get(pid)
        if entry and now - entry.get("ts", 0) < COORD_CACHE_TTL:
            result[pid] = tuple(entry["coords"]) if entry.get("coords") else None
        else:
            need.append(pid)

    if need:
        def _fetch_one(pid):
            try:
                req = urllib.request.Request(
                    f"https://api.upland.me/properties/{pid}",
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read())
                lat, lng = data.get("centerlat"), data.get("centerlng")
                if lat is None or lng is None:
                    return pid, None
                return pid, (float(lat), float(lng))
            except Exception:
                return pid, None

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(_fetch_one, pid) for pid in need]
            for future in concurrent.futures.as_completed(futures):
                pid, coords = future.result()
                result[pid] = coords
                cache[pid] = {"coords": list(coords) if coords else None, "ts": now}
        _save_coord_cache(cache)

    return result


def marker_style(prop_id: str, status: str, owned_ids: set) -> tuple[str, str]:
    """Pin color and label. Owned wins over a stored sale status."""
    if str(prop_id) in owned_ids:
        return "green", "Owned"
    if status == "For sale":
        return "orange", "Missing — for sale"
    if status in (None, "", "Unknown"):
        return "gray", "Sale status not stored"
    return "red", "Missing — not listed"


def _place_name(value) -> str:
    if isinstance(value, dict):
        return value.get("name") or ""
    return str(value or "")


def _hood_name(parsed: dict) -> str:
    req_type = parsed.get("type")
    if req_type in ("neighborhood_city", "neighborhood"):
        return parsed.get("neighborhood") or ""
    if req_type == "place":
        return parsed.get("place") or ""
    return ""


def _listing_candidates(parsed: dict, city_id) -> list:
    """For-sale rows from the local snapshot. Never treats an empty file as a finished crawl."""
    if not city_id:
        return []
    req_type = parsed.get("type")
    neighborhood = None
    street = None
    if req_type in ("neighborhood_city", "neighborhood"):
        neighborhood = parsed.get("neighborhood")
    elif req_type == "place":
        neighborhood = parsed.get("place")
    elif req_type in ("street_city", "street"):
        street = parsed.get("street")
    else:
        return []
    if not neighborhood and not street:
        return []

    scraper_dir = str(SCRIPT_DIR.parent / "scraper")
    if scraper_dir not in sys.path:
        sys.path.insert(0, scraper_dir)
    try:
        from listings_db import DEFAULT_DB, connect, listings_for_requirement
    except Exception:
        return []
    if not DEFAULT_DB.exists():
        return []
    conn = connect(DEFAULT_DB)
    try:
        rows = listings_for_requirement(
            conn,
            city_id=int(city_id),
            neighborhood=neighborhood,
            street=street,
            require_fresh=False,
        )
    except Exception:
        return []
    finally:
        conn.close()
    return rows or []


def _as_marker_source(prop: dict, status: str) -> dict:
    return {
        "id": str(prop.get("id") or ""),
        "address": prop.get("address") or "",
        "status": status,
        "mintPrice": prop.get("mintPrice", prop.get("mint_price")),
        "neighborhood": {"name": _place_name(prop.get("neighborhood"))},
        "city": {"name": _place_name(prop.get("city"))},
        "price": prop.get("price"),
        "currency": prop.get("currency"),
    }


def _owned_candidates(parsed: dict, city_id, owned_ids: set) -> list:
    ids = [str(i) for i in owned_ids]
    hits = _lookup_property_cache_db(ids)
    props = [
        {
            "id": pid,
            "address": meta["address"],
            "neighborhood": meta["neighborhood"],
            "city": meta["city"],
            "mintPrice": 0,
        }
        for pid, meta in hits.items()
    ]
    matched = find_eligible_properties(props, parsed, coll_city_id=city_id)
    return [_as_marker_source(prop, "Owned") for prop in matched]


def _normalize_listing(row: dict) -> dict:
    return _as_marker_source(
        {
            "id": row.get("id"),
            "address": row.get("address") or "",
            "mint_price": row.get("mint_price"),
            "neighborhood": row.get("neighborhood") or "",
            "city": row.get("city") or "",
            "price": row.get("price"),
            "currency": row.get("currency"),
        },
        "For sale",
    )


def gather_map_candidates(parsed: dict, city_id, owned_ids: set) -> tuple[list, str]:
    """
    Return (candidates, coverage). coverage is "neighborhood" or "local".
    This function does not call the developers API.
    """
    owned_ids = {str(i) for i in owned_ids}
    hood = _hood_name(parsed)
    cached = _load_neighborhood_cache(hood, allow_unknown=True) if hood else []
    listings = [_normalize_listing(row) for row in _listing_candidates(parsed, city_id)]
    listing_ids = {row["id"] for row in listings}

    if cached:
        merged = []
        seen = set()
        for prop in cached:
            pid = str(prop.get("id") or prop.get("prop_id") or "")
            if not pid:
                continue
            item = dict(prop)
            item["id"] = pid
            if pid in listing_ids and pid not in owned_ids:
                item["status"] = "For sale"
            merged.append(item)
            seen.add(pid)
        for row in listings:
            if row["id"] not in seen:
                merged.append(row)
        return merged, "neighborhood"

    owned = _owned_candidates(parsed, city_id, owned_ids)
    seen = {row["id"] for row in owned}
    merged = list(owned)
    for row in listings:
        if row["id"] not in seen and row["id"] not in owned_ids:
            merged.append(row)
    return merged, "local"


def _plot_priority(prop: dict, owned_ids: set) -> int:
    pid = str(prop.get("id") or "")
    status = prop.get("status") or ""
    if pid in owned_ids:
        return 0
    if status == "For sale":
        return 1
    if status in ("", "Unknown"):
        return 2
    return 3


def build_collection_map(coll_id: int, owned_ids: set, username: str) -> dict:
    """
    owned_ids: the user's owned property IDs (as strings). username namespaces
    the saved map file.

    Returns {"error": "..."} if the collection can't be mapped, otherwise
    the map url and pin counts. A city scan is not started.
    """
    all_collections = load_collections()
    coll = next((c for c in all_collections if c["id"] == coll_id), None)
    if not coll:
        return {"error": f"Collection {coll_id} not found."}

    parsed = parse_collection_requirement(coll)
    req_type = parsed["type"]
    if req_type not in SUPPORTABLE_TYPES:
        return {"error": f"'{coll['name']}' doesn't have a single geographic scope "
                          f"(requirement type: {req_type}) — can't be shown on a map."}

    city_id = coll.get("cityId")
    owned_ids = {str(i) for i in owned_ids}
    candidates, coverage = gather_map_candidates(parsed, city_id, owned_ids)

    if not candidates:
        return {"error": "No owned or for-sale properties for this collection are in the local "
                          "cache, so the map was not drawn. A city scan was not started."}

    total_candidates = len(candidates)
    truncated = total_candidates > MAX_PLOTTED
    if truncated:
        candidates = sorted(candidates, key=lambda prop: _plot_priority(prop, owned_ids))
    plotted_candidates = candidates[:MAX_PLOTTED]

    prop_ids = [str(c.get("id") or c.get("prop_id")) for c in plotted_candidates]
    coords = _fetch_coords(prop_ids)

    owned_count = listed_count = unlisted_count = unknown_count = 0
    markers = []
    for c in plotted_candidates:
        pid = str(c.get("id") or c.get("prop_id"))
        latlng = coords.get(pid)
        if not latlng:
            continue
        address = c.get("address") or c.get("full_address", "")
        status = c.get("status", "")
        color, label = marker_style(pid, status, owned_ids)
        if color == "green":
            owned_count += 1
        elif color == "orange":
            listed_count += 1
        elif color == "gray":
            unknown_count += 1
        else:
            unlisted_count += 1

        price_bit = ""
        if c.get("currency") and c.get("price") is not None:
            price_bit = f"<br>{html.escape(str(c['currency']))} {html.escape(str(c['price']))}"
        hood_name = html.escape(_place_name(c.get("neighborhood")))
        city_name = html.escape(_place_name(c.get("city")))
        mint = c.get("mintPrice", "?")
        markers.append({
            "lat": latlng[0], "lng": latlng[1], "color": color,
            "popup": (
                f"{html.escape(str(address))}<br>{hood_name}, {city_name}"
                f"<br><b>{html.escape(label)}</b> (mint {html.escape(str(mint))}){price_bit}"
            ),
        })

    if not markers:
        return {"error": "The local properties for this collection have no saved coordinates yet, "
                          "and the location lookup did not return any."}

    if coverage == "local":
        note = LOCAL_NOTE
    elif unknown_count:
        note = UNKNOWN_NOTE
    else:
        note = None

    avg_lat = sum(m["lat"] for m in markers) / len(markers)
    avg_lng = sum(m["lng"] for m in markers) / len(markers)
    m = folium.Map(location=[avg_lat, avg_lng], zoom_start=15, tiles="cartodbpositron")
    for marker in markers:
        folium.CircleMarker(
            location=[marker["lat"], marker["lng"]],
            radius=7,
            color=marker["color"],
            fill=True,
            fill_color=marker["color"],
            fill_opacity=0.85,
            popup=folium.Popup(marker["popup"], max_width=250),
        ).add_to(m)
    legend = (
        '<div style="position:fixed;bottom:12px;left:12px;z-index:9999;background:white;'
        'padding:8px 10px;border-radius:8px;font-size:12px;line-height:1.5;'
        'box-shadow:0 1px 4px rgba(0,0,0,.15);">'
        f"<b>{html.escape(coll['name'])}</b><br>"
        '<span style="color:green">●</span> Owned &nbsp; '
        '<span style="color:darkorange">●</span> For sale &nbsp; '
        '<span style="color:red">●</span> Not listed &nbsp; '
        '<span style="color:gray">●</span> Status not stored'
        "</div>"
    )
    m.get_root().html.add_child(folium.Element(legend))

    MAPS_DIR.mkdir(parents=True, exist_ok=True)
    safe_username = "".join(c if c.isalnum() or c in "-_" else "_" for c in username.lower())
    filename = f"collection_{coll_id}_{safe_username}.html"
    m.save(str(MAPS_DIR / filename))

    return {
        "map_url": f"/maps/{filename}",
        "collection_name": coll["name"],
        "owned_count": owned_count,
        "listed_count": listed_count,
        "unlisted_count": unlisted_count,
        "unknown_count": unknown_count,
        "total_plotted": len(markers),
        "total_candidates": total_candidates,
        "truncated": truncated,
        "coverage": coverage,
        "note": note,
    }
