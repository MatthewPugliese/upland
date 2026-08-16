"""
UplandScope — Collection Map

Given a collection and a user, plots every property in that collection's
geographic scope on a color-coded map: green = you own it, yellow = missing
but currently for sale, red = missing and not currently listed.

Unlike the full neighborhood optimizer maps (map_service.py), this doesn't
match properties to OSM/PLUTO building outlines — it just drops a marker at
each property's centerlat/centerlng (from the live Upland API per-property
endpoint, which the city-listing endpoint used for candidate discovery
doesn't include). That's enough for "where do I still need to buy" at a
glance, and avoids the much heavier building-geocoding pipeline entirely —
this only makes sense for collections whose requirement has a single
geographic scope (street/neighborhood), same caveat as forsale_finder.py.
"""

from __future__ import annotations

import concurrent.futures
import json
import time
import urllib.request
from pathlib import Path

import folium

from collection_optimizer import load_collections, parse_collection_requirement
from forsale_finder import _get_neighborhood_candidates, _get_street_candidates, SUPPORTABLE_TYPES

SCRIPT_DIR = Path(__file__).resolve().parent
MAPS_DIR = SCRIPT_DIR / "maps"
COORD_CACHE_PATH = SCRIPT_DIR / "cache" / "collection_map" / "coord_cache.json"
COORD_CACHE_TTL = 30 * 86400  # coordinates never change
MAX_PLOTTED = 200  # cap per-property coordinate lookups


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


def build_collection_map(coll_id: int, owned_ids: set, username: str) -> dict:
    """
    owned_ids: the user's owned property IDs (as strings) — reuse the set
    already resolved by the Collection Tracker analysis rather than
    re-running the blockchain lookup. username is only used to namespace
    the saved map file.

    Returns {"error": "..."} if the collection can't be mapped, otherwise
    {"map_url": "...", "collection_name": "...", "owned_count": N,
     "listed_count": N, "unlisted_count": N, "total_plotted": N,
     "total_candidates": N, "truncated": bool}.
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
    if req_type in ("street_city", "street"):
        candidates = _get_street_candidates(parsed, city_id)
    else:
        candidates = _get_neighborhood_candidates(parsed, city_id)

    if not candidates:
        return {"error": f"No properties found for '{coll['name']}''s requirement. "
                          f"If this neighborhood/city isn't precached, the live scan only "
                          f"covers the first 3,000 properties in the city and may have missed "
                          f"it entirely — see forsale_finder._get_neighborhood_candidates's "
                          f"docstring for details."}

    total_candidates = len(candidates)
    truncated = total_candidates > MAX_PLOTTED
    if truncated:
        # Candidates come back in whatever order the API/cache returns them —
        # not prioritized by relevance. Put the properties that actually matter
        # (owned, or for-sale-and-missing) first so a truncated map still shows
        # them instead of an arbitrary slice that might miss them entirely.
        def _priority(c):
            pid = str(c.get("id") or c.get("prop_id"))
            if pid in owned_ids:
                return 0
            if c.get("status") == "For sale":
                return 1
            return 2
        candidates = sorted(candidates, key=_priority)
    plotted_candidates = candidates[:MAX_PLOTTED]

    prop_ids = [str(c.get("id") or c.get("prop_id")) for c in plotted_candidates]
    coords = _fetch_coords(prop_ids)

    owned_count = listed_count = unlisted_count = 0
    markers = []
    for c in plotted_candidates:
        pid = str(c.get("id") or c.get("prop_id"))
        latlng = coords.get(pid)
        if not latlng:
            continue
        city_obj = c.get("city") or {}
        hood_obj = c.get("neighborhood") or {}
        address = c.get("address") or c.get("full_address", "")
        status = c.get("status", "")

        if pid in owned_ids:
            color, label = "green", "Owned"
            owned_count += 1
        elif status == "For sale":
            color, label = "orange", "Missing — for sale"
            listed_count += 1
        else:
            color, label = "red", "Missing — not listed"
            unlisted_count += 1

        markers.append({
            "lat": latlng[0], "lng": latlng[1], "color": color,
            "popup": f"{address}<br>{hood_obj.get('name', '')}, {city_obj.get('name', '')}"
                     f"<br><b>{label}</b> (mint {c.get('mintPrice', '?')})",
        })

    if not markers:
        return {"error": f"Couldn't resolve map coordinates for any of '{coll['name']}''s properties."}

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
        "total_plotted": len(markers),
        "total_candidates": total_candidates,
        "truncated": truncated,
    }
