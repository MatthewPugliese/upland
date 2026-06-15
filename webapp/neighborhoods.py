"""
UplandScope — Neighborhood listing and search
"""

import json
import time
from pathlib import Path

from config import CACHE_DIR, NEIGHBORHOOD_LIST_TTL

# Lazy import to avoid loading neighborhood_map at module level
_nm = None


def _get_nm():
    global _nm
    if _nm is None:
        import sys
        from config import NEIGHBORHOOD_MAP_DIR
        sys.path.insert(0, str(NEIGHBORHOOD_MAP_DIR))
        import neighborhood_map as nm
        _nm = nm
    return _nm


_CACHE_FILE = CACHE_DIR / "neighborhoods_all.json"


def get_all_neighborhoods() -> list[dict]:
    """
    Return all neighborhoods across all cities.
    Cached to disk with configurable TTL (default 24h).
    """
    if _CACHE_FILE.exists():
        age = time.time() - _CACHE_FILE.stat().st_mtime
        if age < NEIGHBORHOOD_LIST_TTL:
            with open(_CACHE_FILE) as f:
                return json.load(f)

    nm = _get_nm()
    print("[*] Fetching full neighborhood list from API (this takes a minute)...")
    raw = nm.list_all_neighborhoods()
    results = [
        {
            "id": h["id"],
            "name": h["name"],
            "city_id": h["city_id"],
            "city_name": h["city_name"],
            "area": h.get("area", 0),
        }
        for h in raw
    ]
    results.sort(key=lambda x: (x["city_name"], x["name"]))

    with open(_CACHE_FILE, "w") as f:
        json.dump(results, f)
    print(f"[+] Cached {len(results)} neighborhoods")
    return results


def search_neighborhoods(query: str, limit: int = 20) -> list[dict]:
    """Case-insensitive prefix/substring search on neighborhood names."""
    if not query or len(query) < 2:
        return []

    all_hoods = get_all_neighborhoods()
    q = query.upper()

    # Prefix matches first, then substring
    prefix = []
    substring = []
    for h in all_hoods:
        name_upper = h["name"].upper()
        if name_upper.startswith(q):
            prefix.append(h)
        elif q in name_upper:
            substring.append(h)

    return (prefix + substring)[:limit]
