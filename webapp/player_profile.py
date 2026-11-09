"""Current property ids and collections that are turned on, from the public profile."""

import time

import requests

PROFILE_URL = "https://api.upland.me/profile/{username}"
_CACHE_TTL = 600
_cache: dict[str, tuple[float, dict | None]] = {}


def parse_profile(data: dict) -> dict | None:
    """Pull property ids and active collections out of a profile payload."""
    if not isinstance(data, dict):
        return None
    property_ids = []
    for item in data.get("properties") or []:
        if isinstance(item, dict) and item.get("property_id") is not None:
            property_ids.append(str(item["property_id"]))
        elif isinstance(item, (int, str)) and str(item).strip():
            property_ids.append(str(item).strip())
    if not property_ids:
        return None

    collections = []
    for item in data.get("collections") or []:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        try:
            coll_id = int(item["id"])
        except (TypeError, ValueError):
            continue
        collections.append({
            "id": coll_id,
            "name": item.get("name") or "",
            "city_id": item.get("city_id"),
        })
    return {"property_ids": property_ids, "collections": collections}


def fetch_player_profile(username: str) -> dict | None:
    """Return {property_ids, collections} for a username, or None if the profile is unavailable."""
    key = (username or "").strip()
    if not key:
        return None
    now = time.time()
    hit = _cache.get(key.lower())
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]

    parsed = None
    try:
        response = requests.get(
            PROFILE_URL.format(username=key),
            headers={"User-Agent": "UplandScope/1.0", "Accept": "application/json"},
            timeout=20,
        )
        if response.status_code == 200:
            parsed = parse_profile(response.json())
    except (requests.RequestException, ValueError):
        parsed = None

    _cache[key.lower()] = (now, parsed)
    return parsed
