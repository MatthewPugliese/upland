#!/usr/bin/env python3
"""
Upland Neighborhood Map Generator

Fetches neighborhood boundary and properties from the Upland API, then overlays
OSM building footprints to show approximate property outlines colored by status.

Usage:
    python3 neighborhood_map.py "Inner Richmond"
    python3 neighborhood_map.py "Tenderloin" --city "San Francisco"
    python3 neighborhood_map.py --list-neighborhoods
    python3 neighborhood_map.py --list-neighborhoods --city "Chicago"
    python3 neighborhood_map.py "Tenderloin" --html-only
    python3 neighborhood_map.py "Tenderloin" --refresh-cache

Output:
    <NeighborhoodName>.html  — Interactive map (open in browser)
    <NeighborhoodName>.png   — Static image (requires matplotlib)

Requirements:
    pip install requests folium shapely matplotlib
    pip install contextily        # optional, for tile background in PNG
"""

import argparse
import base64
import json
import math
import os
import sys
import time
from pathlib import Path

import requests

# ─────────────────────────────────────────────────────────────────────────────
# Load credentials from upland-monitor/.env
# ─────────────────────────────────────────────────────────────────────────────

ENV_FILE = Path(__file__).resolve().parent.parent / "upland-monitor" / ".env"


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env(ENV_FILE)

APP_ID = os.environ.get("UPLAND_APP_ID", "")
SECRET = os.environ.get("UPLAND_SECRET", "")

if not APP_ID or not SECRET:
    print(f"[!] Missing credentials. Expected in {ENV_FILE}", file=sys.stderr)
    print("    Set UPLAND_APP_ID and UPLAND_SECRET environment variables", file=sys.stderr)
    sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

UPLAND_API = "https://api.prod.upland.me/developers-api"

# Overpass endpoints tried in order (public mirrors for redundancy)
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

# Locations of the shared property cache from listings.py
_SCRIPT_DIR = Path(__file__).resolve().parent
_PARENT_DIR = _SCRIPT_DIR.parent
MAIN_CACHE_CANDIDATES = [
    _PARENT_DIR / "upland-monitor" / "property_cache.json",
    _PARENT_DIR / "property_cache.json",
    _PARENT_DIR / "property_cache.json.gz",
]

# Color scheme for Upland property statuses
STATUS_COLORS = {
    "For sale":      "#2ECC71",  # green
    "Initial Offer": "#27AE60",  # dark green
    "Locked":        "#95A5A6",  # gray
    "Owned":         "#5A8DB9",  # soft medium blue — owned by others
    "Unlocked":      "#BDC3C7",  # light gray
}
DEFAULT_COLOR  = "#BDC3C7"
USER_COLOR     = "#D4A017"  # gold — properties owned by the tracked username

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
CHAIN_URL = "https://chain-history.upland.me"

# Default username to highlight (override with --username)
DEFAULT_USERNAME = "pugs08"
# EOS blockchain account for the default user (override with --eos-account)
# Different from the Upland display username; find yours at chain-history.upland.me
DEFAULT_EOS_ACCOUNT = "vo1dsqp3qmce"

# Street suffix abbreviations for address normalization
_STREET_ABBREV = {
    "AVENUE": "AVE", "STREET": "ST", "BOULEVARD": "BLVD",
    "DRIVE": "DR", "ROAD": "RD", "LANE": "LN", "COURT": "CT",
    "PLACE": "PL", "CIRCLE": "CIR", "TERRACE": "TER",
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
}

# ─────────────────────────────────────────────────────────────────────────────
# Upland API client
# ─────────────────────────────────────────────────────────────────────────────

def _auth_headers() -> dict:
    token = base64.b64encode(f"{APP_ID}:{SECRET}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
        "User-Agent": "UplandNeighborhoodMapper/1.0",
    }


def upland_get(path: str, params: dict = None) -> dict:
    url = f"{UPLAND_API}{path}"
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_auth_headers(), params=params, timeout=30)
            if r.status_code == 409:
                time.sleep((attempt + 1) * 2)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    return {}


def upland_post(path: str, body: dict) -> dict:
    url = f"{UPLAND_API}{path}"
    for attempt in range(3):
        try:
            r = requests.post(url, headers=_auth_headers(), json=body, timeout=30)
            if r.status_code == 409:
                time.sleep((attempt + 1) * 2)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == 2:
                return {}
            time.sleep(2 ** attempt)
    return {}

# ─────────────────────────────────────────────────────────────────────────────
# Find neighborhood
# ─────────────────────────────────────────────────────────────────────────────

def list_all_neighborhoods(city_filter: str = None) -> list:
    """Return all neighborhoods across all cities, with city_id/city_name attached."""
    cities = upland_get("/cities").get("cities", [])
    results = []
    for city in cities:
        if city_filter and city_filter.lower() not in city["name"].lower():
            continue
        hoods = upland_get("/neighborhoods", {"cityId": city["id"]}).get("results", [])
        for h in hoods:
            results.append({**h, "city_id": city["id"], "city_name": city["name"]})
        time.sleep(0.1)
    return results


def find_neighborhood(name: str, city_hint: str = None) -> dict:
    """
    Find a neighborhood by name (case-insensitive, partial match OK).
    Returns the neighborhood dict with city_id and city_name attached.
    """
    print(f"[*] Searching for neighborhood: '{name}'")
    hoods = list_all_neighborhoods(city_filter=city_hint)

    # Exact match first
    for h in hoods:
        if h["name"].upper() == name.upper():
            print(f"[+] Found '{h['name']}' in {h['city_name']}")
            return h

    # Partial match
    matches = [h for h in hoods if name.upper() in h["name"].upper()]
    if len(matches) == 1:
        print(f"[~] Using partial match: '{matches[0]['name']}' in {matches[0]['city_name']}")
        return matches[0]
    elif len(matches) > 1:
        print(f"[?] Multiple matches — using first:")
        for h in matches:
            print(f"    - {h['name']} ({h['city_name']})")
        return matches[0]

    raise ValueError(
        f"Neighborhood '{name}' not found. "
        "Use --list-neighborhoods to see all available neighborhoods."
    )

# ─────────────────────────────────────────────────────────────────────────────
# Fetch Upland properties
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Property cache (from listings.py / upland-monitor)
# ─────────────────────────────────────────────────────────────────────────────

_main_cache: dict | None = None  # {prop_id_str: full_address_str}


def _load_main_cache() -> dict:
    """
    Load the large property cache built by listings.py.
    Format: {property_id: "ADDRESS, NEIGHBORHOOD, City"}
    Tries several candidate paths, handles both .json and .json.gz.
    """
    global _main_cache
    if _main_cache is not None:
        return _main_cache

    for candidate in MAIN_CACHE_CANDIDATES:
        if not candidate.exists():
            continue
        print(f"[*] Loading property cache from {candidate} …", end=" ", flush=True)
        t0 = time.time()
        try:
            if candidate.suffix == ".gz":
                import gzip
                with gzip.open(candidate, "rt", encoding="utf-8") as f:
                    _main_cache = json.load(f)
            else:
                with open(candidate, encoding="utf-8") as f:
                    _main_cache = json.load(f)
            elapsed = time.time() - t0
            print(f"{len(_main_cache):,} properties loaded in {elapsed:.1f}s")
            return _main_cache
        except Exception as e:
            print(f"failed ({e})")

    print("[!] No property cache found — will use API only")
    _main_cache = {}
    return _main_cache


def _properties_from_main_cache(neighborhood_name: str) -> list[dict]:
    """
    Search the main property cache for entries belonging to a neighborhood.
    Cache entries: "ADDRESS, NEIGHBORHOOD, City"  or  "ADDRESS, City"
    Returns list of minimal prop dicts: {id, address, status, neighborhood_name}
    """
    cache = _load_main_cache()
    if not cache:
        return []

    target = neighborhood_name.upper()
    results = []
    for prop_id, full_addr in cache.items():
        # The neighborhood name is the 2nd comma-delimited segment when present
        parts = [p.strip() for p in full_addr.split(",")]
        if len(parts) >= 2 and parts[1].upper() == target:
            results.append({
                "id": prop_id,
                "address": parts[0],
                "status": None,          # will be enriched from API
                "mintPrice": None,
                "collection": None,
                "_from_cache": True,
            })
    return results


def _enrich_from_api(cached_props: list, city_id: int, neighborhood_id: int) -> list:
    """
    Use street-name textSearch queries to fetch live API data (status, mintPrice)
    for a set of cached properties, then merge by address.
    Matches by address string (not neighborhood.id — that field is often null in the API).
    """
    if not cached_props:
        return cached_props

    # Collect unique search terms: distinctive part of each street name (no suffix)
    search_terms: set[str] = set()
    for p in cached_props:
        addr = p["address"].upper().strip()
        parts = addr.split(maxsplit=1)
        if len(parts) > 1:
            tokens = [_STREET_ABBREV.get(t, t) for t in parts[1].split()]
            normed = " ".join(tokens)
            term = _street_to_search_term(normed)
            if term:
                search_terms.add(term)

    print(f"[*] Fetching live status for {len(cached_props)} properties "
          f"via {len(search_terms)} API searches …")

    # Build lookup: normalized_address → live prop
    live_by_addr: dict[str, dict] = {}
    for i, term in enumerate(sorted(search_terms), 1):
        print(f"  [{i}/{len(search_terms)}] '{term}'    ", end="\r", flush=True)
        api_props = _search_city_by_street(city_id, term)
        for lp in api_props:
            key = lp.get("address", "").upper().strip()
            live_by_addr[key] = lp
        time.sleep(0.2)

    hits = sum(1 for cp in cached_props if cp["address"].upper().strip() in live_by_addr)
    print(f"\n[+] Matched {hits}/{len(cached_props)} cached properties to live API data")

    # Merge: live data overrides cached; missing ones keep cached with status=Unknown
    merged = []
    for cp in cached_props:
        key = cp["address"].upper().strip()
        lp = live_by_addr.get(key)
        if lp:
            merged.append(lp)
        else:
            merged.append({**cp, "status": "Unknown"})
    return merged


# ─────────────────────────────────────────────────────────────────────────────
# User property lookup
# ─────────────────────────────────────────────────────────────────────────────

def _blockchain_user_properties(username: str, eos_account: str, cache_path: Path) -> set[str]:
    """
    Determine current property holdings via the Upland blockchain (Hyperion API).

    Strategy: query the most recent `n31` (collection) actions for the EOS account.
    Each `n31` has a `p55` field listing all properties the user currently holds.
    A single collection round fires many `n31` actions within a few seconds, each
    with up to 50 property IDs, together covering the full current portfolio.

    The Hyperion API requires the EOS account name (e.g. 'vo1dsqp3qmce'), not the
    Upland display username (e.g. 'pugs08').  Pass --eos-account to configure it.

    Results are cached in `cache_path` (1-hour TTL).
    """
    import time as _time

    # Load existing cache
    if cache_path.exists():
        age = _time.time() - cache_path.stat().st_mtime
        if age < 3600:  # 1-hour TTL
            with open(cache_path) as f:
                data = json.load(f)
            ids = {str(i) for i in data.get("owned", [])}
            if ids:
                print(f"[+] Blockchain cache: {len(ids)} properties for '{username}' (cached)")
                return ids

    if not eos_account:
        print(f"[~] No EOS account provided — cannot look up '{username}' on blockchain")
        print(f"    Pass --eos-account <your_eos_account> to enable gold highlighting")
        return set()

    print(f"[*] Querying blockchain for '{username}' (EOS: {eos_account}) …")

    base = f"{CHAIN_URL}/v2/history/get_actions"

    # ── Step 1: find the timestamp of the most recent n31 action ────────────
    try:
        r = requests.get(base, params={"account": eos_account, "filter": "playuplandme:n31",
                                        "limit": 1, "sort": "desc"},
                         headers={"User-Agent": "UplandNeighborhoodMapper/1.0"}, timeout=30)
        r.raise_for_status()
        recent = r.json().get("actions", [])
    except Exception as e:
        print(f"[!] Blockchain query error: {e}")
        return set()

    if not recent:
        print(f"[~] No n31 collection actions found for '{eos_account}'")
        return set()

    latest_ts = recent[0]["@timestamp"]  # e.g. "2026-03-01T01:37:27.500"
    # Parse to seconds since epoch for window comparison
    from datetime import datetime, timezone
    try:
        latest_dt = datetime.fromisoformat(latest_ts.replace("Z", "+00:00"))
    except ValueError:
        latest_dt = datetime.strptime(latest_ts[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc)
    latest_epoch = latest_dt.timestamp()

    # ── Step 2: pull all n31 actions within 120 s of the latest one ─────────
    # (a single collection round fires all batches within a few seconds)
    owned: set[str] = set()
    skip = 0
    total_seen = 0

    while True:
        params = {"account": eos_account, "filter": "playuplandme:n31",
                  "limit": 100, "skip": skip, "sort": "desc"}
        try:
            r = requests.get(base, params=params,
                             headers={"User-Agent": "UplandNeighborhoodMapper/1.0"}, timeout=30)
            r.raise_for_status()
            payload = r.json()
        except Exception as e:
            print(f"[!] Blockchain query error: {e}")
            break

        actions = payload.get("actions", [])
        if not actions:
            break

        stop = False
        for action in actions:
            ts = action.get("@timestamp", "")
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                dt = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(
                    tzinfo=timezone.utc)
            if (latest_epoch - dt.timestamp()) > 120:
                stop = True
                break
            data_raw = action.get("act", {}).get("data", {})
            p55 = data_raw.get("p55", [])
            owned.update(str(x) for x in p55)

        total_seen += len(actions)
        print(f"    blockchain n31: {len(owned)} properties found so far …", end="\r")

        if stop or len(actions) < 100:
            break
        skip += 100
        _time.sleep(0.1)

    print()  # newline after \r progress
    print(f"[+] Blockchain: {len(owned)} currently owned properties for '{username}'")

    # Save cache
    with open(cache_path, "w") as f:
        json.dump({"username": username, "eos_account": eos_account,
                   "owned": sorted(owned)}, f)

    return owned


def get_user_property_ids(city_id: int, username: str, eos_account: str = "",
                           user_props_file: Path = None,
                           blockchain_cache: Path = None) -> set[str]:
    """
    Return a set of property ID strings owned by `username`.

    Priority:
      1. --user-props-file  (JSON array of IDs, most reliable)
      2. Blockchain history (Hyperion API via EOS account) — net bought minus sold
      3. Empty set (when no EOS account is known)
    """
    if not username:
        return set()

    # 1. Manual file override
    if user_props_file and user_props_file.exists():
        with open(user_props_file) as f:
            ids = json.load(f)
        print(f"[+] Loaded {len(ids)} user property IDs from {user_props_file.name}")
        return {str(i) for i in ids}

    # 2. Blockchain history (requires EOS account name)
    if blockchain_cache is None:
        blockchain_cache = _SCRIPT_DIR / f"{username}_blockchain_cache.json"
    return _blockchain_user_properties(username, eos_account, blockchain_cache)


# ─────────────────────────────────────────────────────────────────────────────
# Geocoding (Nominatim) — places unmatched Upland properties on the map
# ─────────────────────────────────────────────────────────────────────────────

def _nominatim_geocode(address: str, city: str) -> tuple[float, float] | None:
    """Single Nominatim lookup. Returns (lat, lon) or None."""
    try:
        r = requests.get(
            NOMINATIM_URL,
            params={"q": f"{address}, {city}", "format": "json", "limit": 1},
            headers={"User-Agent": "UplandNeighborhoodMapper/1.0"},
            timeout=10,
        )
        r.raise_for_status()
        results = r.json()
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception:
        pass
    return None


def geocode_props(props: list, city_name: str, cache_path: Path) -> dict[str, list]:
    """
    Geocode a list of property dicts via Nominatim.
    Returns {ADDRESS_UPPER: [lat, lon]} (None values for failures).
    Results are cached in `cache_path` so subsequent runs are instant.
    """
    geocache: dict[str, list] = {}
    if cache_path.exists():
        with open(cache_path) as f:
            geocache = json.load(f)

    to_geocode = [p for p in props if p["address"].upper().strip() not in geocache]

    if not to_geocode:
        hits = sum(1 for v in geocache.values() if v is not None)
        print(f"[+] Geocode cache: {hits}/{len(geocache)} addresses resolved")
        return geocache

    print(f"[*] Geocoding {len(to_geocode)} addresses via Nominatim (1 req/sec) …")
    for i, prop in enumerate(to_geocode, 1):
        key = prop["address"].upper().strip()
        result = _nominatim_geocode(prop["address"], city_name)
        geocache[key] = list(result) if result else None
        print(f"  [{i}/{len(to_geocode)}] {prop['address'][:40]}    ", end="\r", flush=True)
        time.sleep(1.1)  # Nominatim ToS: max 1 req/sec

    print()
    with open(cache_path, "w") as f:
        json.dump(geocache, f)

    hits = sum(1 for v in geocache.values() if v is not None)
    print(f"[+] Geocoding done: {hits}/{len(geocache)} addresses resolved")
    return geocache


def _addr_nodes_to_geocode_map(addr_nodes: list) -> dict[str, list]:
    """
    Build a fast coordinate lookup from OSM address nodes already fetched
    from Overpass, avoiding extra Nominatim calls.

    Returns {ADDRESS_UPPER: [lat, lon]}, e.g. {"45 VERA ST": [40.584, -74.097]}.
    Normalizes street abbreviations to match Upland address format.
    Also inserts base-number variants so '45A VERA ST' covers '45 VERA ST'.
    """
    result: dict[str, list] = {}
    for node in addr_nodes:
        num = node["house_num"].strip()
        street_raw = node["street"].strip()
        if not num or not street_raw:
            continue
        tokens = [_STREET_ABBREV.get(t, t) for t in street_raw.upper().split()]
        street = " ".join(tokens)
        coords = [node["lat"], node["lon"]]
        key = f"{num} {street}"
        result[key] = coords
        # Also add a base-number entry so "45A VERA ST" covers lookup for "45 VERA ST"
        base = num.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
        if base and base != num:
            base_key = f"{base} {street}"
            result.setdefault(base_key, coords)
    return result


def _overpass_query(query: str, timeout: int = 120) -> dict | None:
    """Try each Overpass endpoint in turn. Returns parsed JSON or None."""
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            r = requests.post(endpoint, data={"data": query}, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"[~] Overpass {endpoint.split('/')[2]} failed: {e}")
    return None


def _get_osm_street_names(boundary_coords: list) -> list[str]:
    """
    Query Overpass for all named streets inside the neighborhood boundary.
    Returns normalized street name strings (uppercase, abbreviated).
    """
    poly_str = _poly_to_overpass_str(boundary_coords)
    query = f"""
[out:json][timeout:60];
way["highway"]["name"](poly:"{poly_str}");
out tags qt;
"""
    data = _overpass_query(query, timeout=90)
    if data is None:
        print(f"[!] Could not get OSM streets from any Overpass endpoint")
        return []

    streets: set[str] = set()
    for elem in (data or {}).get("elements", []):
        name = elem.get("tags", {}).get("name", "").strip().upper()
        if not name:
            continue
        # Normalize abbreviations
        tokens = [_STREET_ABBREV.get(t, t) for t in name.split()]
        streets.add(" ".join(tokens))

    return sorted(streets)


# Street type suffixes to strip when building a textSearch term.
# The Upland API tokenizes on spaces and does OR-matching, so "STANYAN ST"
# would match every property containing "ST". Searching just "STANYAN" is precise.
_STRIP_SUFFIXES = {
    "ST", "AVE", "BLVD", "DR", "RD", "LN", "CT", "PL", "CIR",
    "TER", "WAY", "HWY", "FWY", "PKWY", "ESPL", "EXPY",
}


def _street_to_search_term(normalized_street: str) -> str:
    """Strip trailing type suffix so the API search is specific, not broad."""
    parts = normalized_street.split()
    if parts and parts[-1] in _STRIP_SUFFIXES:
        parts = parts[:-1]
    return " ".join(parts) if parts else normalized_street


def _search_city_by_street(city_id: int, street_search_term: str) -> list:
    """
    Fetch all Upland properties matching a street search term in a city.
    Does NOT filter by neighborhood — caller matches by address instead.
    """
    from urllib.parse import quote as _quote
    found = {}
    page = 1
    while True:
        encoded = _quote(street_search_term, safe="")
        url = (
            f"{UPLAND_API}/properties"
            f"?cityId={city_id}"
            f"&currentPage={page}"
            f"&pageSize=100"
            f"&textSearch={encoded}"
        )
        import requests as _req
        for attempt in range(3):
            try:
                r = _req.get(url, headers=_auth_headers(), timeout=30)
                if r.status_code == 409:
                    time.sleep((attempt + 1) * 2)
                    continue
                r.raise_for_status()
                data = r.json()
                break
            except Exception:
                if attempt == 2:
                    data = {}
                time.sleep(2)
        results = data.get("results", [])
        total = data.get("totalResults", 0)
        for p in results:
            found[p["id"]] = p
        if len(results) < 100 or page >= 99 or total <= page * 100:
            break
        page += 1
        time.sleep(0.25)
    return list(found.values())


def get_neighborhood_properties(
    city_id: int,
    neighborhood_id: int,
    neighborhood_name: str,
    cache_path: Path,
    boundary_coords: list = None,
) -> list:
    """
    Fetch all Upland properties for a neighborhood, cached locally.

    Priority order:
      1. Local neighborhood cache (fast, from previous run)
      2. Shared property_cache.json from listings.py (large cache, local)
         → extract properties by neighborhood name in address string
         → enrich with live API data (status, mintPrice, collection)
      3. OSM street names → Upland textSearch (for large cities w/o shared cache)
      4. Simple page scan (fallback for small cities)
    """
    # ── 1. Local neighborhood cache ──────────────────────────────────────────
    if cache_path.exists():
        with open(cache_path) as f:
            cached = json.load(f)
        if cached:
            print(f"[+] Loaded {len(cached)} properties from local cache ({cache_path.name})")
            return cached
        cache_path.unlink()  # empty file — ignore and re-fetch

    # ── 2. Shared property cache (listings.py) ───────────────────────────────
    cached_props = _properties_from_main_cache(neighborhood_name)
    if cached_props:
        print(f"[+] Found {len(cached_props)} properties in shared cache")
        props = _enrich_from_api(cached_props, city_id, neighborhood_id)
    else:
        # ── 3 & 4: API-only paths ─────────────────────────────────────────────
        print("[*] Shared cache miss — fetching from Upland API...")
        first = upland_get("/properties", {"cityId": city_id, "currentPage": 1, "pageSize": 100})
        total = first.get("totalResults", 0)
        print(f"    City has {total:,} total properties")

        all_api: dict[str, dict] = {}

        if boundary_coords and total > 9900:
            # ── 3. OSM street names ───────────────────────────────────────────
            print("[*] Large city — getting streets from OSM for targeted search")
            streets = _get_osm_street_names(boundary_coords)
            if streets:
                search_terms = sorted({_street_to_search_term(s) for s in streets if s})
                print(f"[+] {len(search_terms)} search terms from {len(streets)} streets")
                for i, s in enumerate(search_terms, 1):
                    print(f"  [{i}/{len(search_terms)}] '{s}'    ", end="\r", flush=True)
                    for p in _search_city_by_street(city_id, s):
                        nh = p.get("neighborhood") or {}
                        if nh.get("id") == neighborhood_id \
                                or nh.get("name", "").upper() == neighborhood_name.upper():
                            all_api[str(p["id"])] = p
                    time.sleep(0.25)
                print()
            else:
                print("[!] No OSM streets — using page scan (may be incomplete)")
                boundary_coords = None  # fall through to scan

        if not boundary_coords or total <= 9900:
            # ── 4. Page scan ──────────────────────────────────────────────────
            max_pages = min(99, math.ceil(total / 100))
            for p in first.get("results", []):
                if p.get("neighborhood", {}).get("id") == neighborhood_id:
                    all_api[str(p["id"])] = p
            for page in range(2, max_pages + 1):
                data = upland_get("/properties", {
                    "cityId": city_id, "currentPage": page, "pageSize": 100,
                })
                results = data.get("results", [])
                for p in results:
                    if p.get("neighborhood", {}).get("id") == neighborhood_id:
                        all_api[str(p["id"])] = p
                if not results:
                    break
                print(f"  Scanning {page}/{max_pages} ({len(all_api)} found)    ",
                      end="\r", flush=True)
                time.sleep(0.2)
            print()

        props = list(all_api.values())

    print(f"[+] {len(props)} properties in '{neighborhood_name}'")
    if props:
        print(f"[i] Property fields: {list(props[0].keys())}")

    with open(cache_path, "w") as f:
        json.dump(props, f)

    return props

# ─────────────────────────────────────────────────────────────────────────────
# NYC MapPLUTO — exact tax lot (parcel) boundaries used by Upland for NYC
# ─────────────────────────────────────────────────────────────────────────────

# Upland city names that are NYC boroughs — use MapPLUTO for these
_NYC_CITY_NAMES = {"manhattan", "brooklyn", "staten island", "bronx", "queens"}


def _is_nyc(city_name: str) -> bool:
    return city_name.lower().strip() in _NYC_CITY_NAMES


_PLUTO_URL = ("https://services5.arcgis.com/GfwWNkhOj9bNBqoJ/ArcGIS/rest/services"
              "/MAPPLUTO/FeatureServer/0/query")


def get_nyc_pluto_parcels(boundary_coords: list, cache_path: Path = None) -> list:
    """
    Fetch NYC MapPLUTO tax lot polygons via the NYC ArcGIS REST service.

    Each MapPLUTO record = one tax lot = one Upland property parcel.
    These parcel polygons match exactly what Upland shows in the game for NYC
    properties — the legal lot boundary, not just the building footprint.

    Returns list of parcel dicts compatible with the `buildings` list format.
    Cached for 7 days (parcels rarely change).
    """
    if cache_path and cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < 86400 * 7:
            with open(cache_path) as f:
                data = json.load(f)
            if data:
                print(f"[+] MapPLUTO cache: {len(data)} parcels")
                return data

    ring = (boundary_coords[0]
            if isinstance(boundary_coords[0][0], (list, tuple))
            else boundary_coords)
    lons = [pt[0] for pt in ring]
    lats = [pt[1] for pt in ring]
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)

    print("[*] Fetching NYC MapPLUTO parcel boundaries (ArcGIS REST)...")
    import json as _json
    bbox = _json.dumps({
        "xmin": min_lon, "ymin": min_lat,
        "xmax": max_lon, "ymax": max_lat,
        "spatialReference": {"wkid": 4326},
    })

    parcels = []
    offset = 0
    while True:
        params = {
            "geometry": bbox,
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "Address,BBL",
            "returnGeometry": "true",
            "resultRecordCount": 2000,
            "resultOffset": offset,
            "f": "geojson",
        }
        try:
            r = requests.get(_PLUTO_URL, params=params,
                             headers={"User-Agent": "UplandNeighborhoodMapper/1.0"},
                             timeout=60)
            r.raise_for_status()
            geojson = r.json()
        except Exception as e:
            print(f"[!] MapPLUTO API error: {e}")
            break

        features = geojson.get("features", [])
        if not features:
            break

        for feat in features:
            props = feat.get("properties", {})
            geom = feat.get("geometry") or {}
            gtype = geom.get("type", "")

            if gtype == "Polygon":
                ring_list = [geom["coordinates"][0]]
            elif gtype == "MultiPolygon":
                ring_list = [part[0] for part in geom["coordinates"]]
            else:
                continue

            # Use the largest ring
            ring_coords = max(ring_list, key=len)
            coords = [(c[0], c[1]) for c in ring_coords]  # (lon, lat)
            if len(coords) < 3:
                continue

            centroid_lon = sum(c[0] for c in coords) / len(coords)
            centroid_lat = sum(c[1] for c in coords) / len(coords)

            # Normalize: "45 VERA STREET" → house_num="45", street="VERA ST"
            raw_addr = (props.get("Address") or "").strip().upper()
            addr_parts = raw_addr.split(maxsplit=1)
            house_num = addr_parts[0] if addr_parts else ""
            street_raw = addr_parts[1] if len(addr_parts) > 1 else ""
            tokens = [_STREET_ABBREV.get(t, t) for t in street_raw.split()]
            street = " ".join(tokens)

            parcels.append({
                "osm_id": f"pluto_{props.get('BBL', '')}",
                "coords": coords,
                "centroid_lat": centroid_lat,
                "centroid_lon": centroid_lon,
                "house_num": house_num,
                "street": street,
                "name": "",
                "tags": {"building": "yes"},
                "source": "pluto",
            })

        print(f"  MapPLUTO: {len(parcels)} parcels so far …", end="\r", flush=True)
        if len(features) < 2000:
            break
        offset += 2000
        time.sleep(0.3)

    print(f"\n[+] MapPLUTO: {len(parcels)} parcels fetched")

    if cache_path and parcels:
        with open(cache_path, "w") as f:
            json.dump(parcels, f)

    return parcels


# ─────────────────────────────────────────────────────────────────────────────
# Overpass API — OSM building footprints
# ─────────────────────────────────────────────────────────────────────────────

def _poly_to_overpass_str(boundary_coords) -> str:
    """
    Convert Upland boundary coords (GeoJSON format: [[[lon, lat], ...]])
    to Overpass poly string format: "lat1 lon1 lat2 lon2 ..."
    """
    # Handle nested rings: boundary[0] is the outer ring
    ring = boundary_coords[0] if isinstance(boundary_coords[0][0], (list, tuple)) else boundary_coords
    parts = []
    for pt in ring:
        lon, lat = pt[0], pt[1]
        parts.extend([str(round(lat, 7)), str(round(lon, 7))])
    return " ".join(parts)


def get_overpass_buildings(boundary_coords: list) -> tuple[list, list]:
    """
    Fetch building footprints and address nodes from OpenStreetMap via Overpass API.
    Returns (buildings, addr_nodes) where buildings is a list of building dicts with
    polygon coords and address tags, and addr_nodes is a list of standalone address nodes.
    """
    print("[*] Fetching building footprints from OpenStreetMap (Overpass)...")
    poly_str = _poly_to_overpass_str(boundary_coords)

    query = f"""
[out:json][timeout:120];
(
  way["building"](poly:"{poly_str}");
  way["demolished:building"](poly:"{poly_str}");
  way["addr:housenumber"][!"building"][!"demolished:building"](poly:"{poly_str}");
  node["addr:housenumber"](poly:"{poly_str}");
);
out body;
>;
out skel qt;
"""

    data = _overpass_query(query, timeout=150)
    if data is None:
        print("[!] All Overpass endpoints failed — no building outlines")
        return [], []

    # Build node coordinate lookup
    nodes = {}
    for elem in data.get("elements", []):
        if elem["type"] == "node":
            nodes[elem["id"]] = (elem["lat"], elem["lon"])

    buildings = []
    for elem in data.get("elements", []):
        if elem["type"] != "way":
            continue
        tags = elem.get("tags", {})
        # Accept: has building tag, OR has demolished:building, OR has addr:housenumber polygon
        if not (tags.get("building") or tags.get("demolished:building")
                or tags.get("addr:housenumber")):
            continue

        # Reconstruct polygon coordinates [lon, lat] (GeoJSON order)
        coords = []
        for nid in elem.get("nodes", []):
            if nid in nodes:
                lat, lon = nodes[nid]
                coords.append((lon, lat))

        if len(coords) < 3:
            continue

        # Centroid
        centroid_lon = sum(c[0] for c in coords) / len(coords)
        centroid_lat = sum(c[1] for c in coords) / len(coords)

        buildings.append({
            "osm_id": elem["id"],
            "coords": coords,           # list of (lon, lat)
            "centroid_lat": centroid_lat,
            "centroid_lon": centroid_lon,
            "house_num": tags.get("addr:housenumber", ""),
            "street": tags.get("addr:street", "").upper(),
            "name": tags.get("name", ""),
            "tags": tags,
        })

    # Parse address nodes (standalone OSM nodes with addr:housenumber)
    addr_nodes = []
    for elem in data.get("elements", []):
        if elem["type"] != "node":
            continue
        tags = elem.get("tags", {})
        if not tags.get("addr:housenumber"):
            continue
        addr_nodes.append({
            "osm_id": elem["id"],
            "lat": elem["lat"],
            "lon": elem["lon"],
            "house_num": tags.get("addr:housenumber", ""),
            "street": tags.get("addr:street", "").upper(),
        })

    # Fusion pass: fuse addr_nodes to nearby buildings that lack an address
    fused = 0

    # Building types that are NOT residential — skip fusing addresses onto these
    _SKIP_BUILDING_TYPES = {
        "church", "cathedral", "chapel", "shrine", "mosque", "synagogue",
        "temple", "religious", "school", "university", "college", "dormitory",
        "hospital", "commercial", "office", "industrial", "warehouse",
        "retail", "supermarket", "kiosk", "garage", "garages", "parking",
        "service", "stable", "fire_station", "police", "civic", "government",
    }

    def _addr_base(house_num: str) -> str:
        """Strip trailing letters from house number for prefix matching."""
        return house_num.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")

    def _point_in_poly(lon: float, lat: float, coords: list) -> bool:
        """Ray-casting point-in-polygon test. coords = list of (lon, lat)."""
        n = len(coords)
        inside = False
        j = n - 1
        for i in range(n):
            xi, yi = coords[i]
            xj, yj = coords[j]
            if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
                inside = not inside
            j = i
        return inside

    for addr_node in addr_nodes:
        an_num = addr_node["house_num"].strip()
        an_street = addr_node["street"].strip()
        an_base = _addr_base(an_num)

        # Check if any building already has a matching address
        already_matched = False
        for b in buildings:
            b_num = b["house_num"].strip()
            b_street = b["street"].strip()
            if not b_num:
                continue
            b_base = _addr_base(b_num)
            num_match = (b_num == an_num) or (b_base == an_base and an_base != "")
            street_match = (b_street == an_street) or (b_street in an_street) or (an_street in b_street)
            if num_match and street_match:
                already_matched = True
                break

        if already_matched:
            continue

        an_lat = addr_node["lat"]
        an_lon = addr_node["lon"]
        lat_rad = math.radians(an_lat)

        # Strategy 1: address node is INSIDE the building polygon (most precise)
        contained_in = None
        for b in buildings:
            if b["house_num"] != "":
                continue  # already has an address
            btype = b["tags"].get("building", "").lower()
            if btype in _SKIP_BUILDING_TYPES:
                continue
            if _point_in_poly(an_lon, an_lat, b["coords"]):
                contained_in = b
                break

        if contained_in is not None:
            contained_in["house_num"] = addr_node["house_num"]
            contained_in["street"] = addr_node["street"]
            fused += 1
            continue

        # Strategy 2: nearest building centroid within 15 m (much tighter than before)
        # Only fuse to residential-type buildings
        best_dist = float("inf")
        best_bldg = None
        for b in buildings:
            if b["house_num"] != "":
                continue
            btype = b["tags"].get("building", "").lower()
            if btype in _SKIP_BUILDING_TYPES:
                continue
            dlat = b["centroid_lat"] - an_lat
            dlon = b["centroid_lon"] - an_lon
            dist = math.sqrt((dlat * 111000) ** 2 + (dlon * 111000 * math.cos(lat_rad)) ** 2)
            if dist < best_dist:
                best_dist = dist
                best_bldg = b

        if best_bldg is not None and best_dist <= 15:
            best_bldg["house_num"] = addr_node["house_num"]
            best_bldg["street"] = addr_node["street"]
            fused += 1

    print(f"[+] Fused {fused} address nodes to nearby buildings")
    print(f"[+] Found {len(buildings)} OSM footprints + {len(addr_nodes)} address nodes in neighborhood")
    return buildings, addr_nodes

# ─────────────────────────────────────────────────────────────────────────────
# Upland game buildings (structures placed by players)
# ─────────────────────────────────────────────────────────────────────────────

def get_osm_road_geometry(boundary_coords: list) -> list:
    """
    Fetch road polylines from OSM for drawing a tile-free background map.
    Returns a list of polylines: each is a list of [lat, lon] pairs.
    """
    poly_str = _poly_to_overpass_str(boundary_coords)
    query = f"""
[out:json][timeout:60];
way["highway"~"^(residential|tertiary|secondary|primary|unclassified|service|living_street|pedestrian|footway|path|steps|trunk|motorway)$"](poly:"{poly_str}");
out geom qt;
"""
    data = _overpass_query(query, timeout=90)
    if data is None:
        print("[!] Could not fetch road geometry from Overpass")
        return []

    polylines = []
    for elem in (data or {}).get("elements", []):
        if elem.get("type") != "way":
            continue
        geom = elem.get("geometry", [])
        if len(geom) < 2:
            continue
        polylines.append([[pt["lat"], pt["lon"]] for pt in geom])

    print(f"[+] Fetched {len(polylines)} road segments from OSM")
    return polylines


def get_upland_structures(boundary_coords: list) -> set:
    # Kept for compatibility — superseded by get_upland_property_structures()
    return set()


def get_upland_property_structures(props: list, cache_path: Path) -> dict:
    """
    Fetch game structure data for every property via the Upland public API.
    Returns {prop_id_str: [{"buildingName": ..., "buildingType": ..., ...}, ...]}

    The public API at https://api.upland.me/properties/{id} returns a `buildings`
    array listing every structure currently placed on that property.
    Results are cached for 24 hours.
    """
    import threading
    import concurrent.futures

    cache: dict = {}
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < 86400:
            with open(cache_path) as f:
                cache = json.load(f)
            has_structs = sum(1 for v in cache.values() if v)
            print(f"[+] Structure cache: {len(cache)} properties, {has_structs} with structures")
            return cache

    print(f"[*] Fetching Upland structure data for {len(props)} properties …")
    lock = threading.Lock()
    done = [0]

    def fetch_one(prop):
        pid = str(prop["id"])
        try:
            r = requests.get(f"https://api.upland.me/properties/{prop['id']}",
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            if r.status_code == 200:
                data = r.json()
                bldgs = data.get("buildings") or []
                return pid, [
                    {"buildingName": b.get("buildingName", ""),
                     "buildingType": b.get("buildingType", ""),
                     "constructionStatus": b.get("constructionStatus", "")}
                    for b in bldgs
                ]
        except Exception:
            pass
        return pid, []

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_one, p): p for p in props}
        for future in concurrent.futures.as_completed(futures):
            pid, structs = future.result()
            cache[pid] = structs
            with lock:
                done[0] += 1
                has = sum(1 for v in cache.values() if v)
                print(f"  [{done[0]}/{len(props)}] {has} with structures …",
                      end="\r", flush=True)

    print()
    has_structs = sum(1 for v in cache.values() if v)
    print(f"[+] Structures: {has_structs}/{len(props)} properties have game structures")

    with open(cache_path, "w") as f:
        json.dump(cache, f)

    return cache

# ─────────────────────────────────────────────────────────────────────────────
# Address matching: Upland properties ↔ OSM buildings
# ─────────────────────────────────────────────────────────────────────────────

def _normalize(addr: str) -> tuple[str, str]:
    """
    Split address into (house_number, normalized_street).
    e.g. "1467 28TH AVE" → ("1467", "28TH AVE")
    """
    addr = addr.upper().strip()
    parts = addr.split(maxsplit=1)
    if not parts:
        return "", addr

    house_num = parts[0] if parts[0][0].isdigit() else ""
    street_raw = parts[1] if len(parts) > 1 else addr

    # Expand and re-abbreviate tokens
    tokens = street_raw.split()
    tokens = [_STREET_ABBREV.get(t, t) for t in tokens]
    street = " ".join(tokens)

    return house_num, street


def _merge_building_coords(buildings: list) -> list:
    """
    Given multiple OSM building dicts for the same Upland property, return a
    single merged polygon (convex hull of all vertices).  This mimics the
    game's parcel/lot boundary which covers the whole lot regardless of how
    many structures sit on it.

    Falls back to the largest single building if Shapely is unavailable.
    """
    if len(buildings) == 1:
        return buildings[0]["coords"]

    try:
        from shapely.geometry import Polygon, MultiPolygon
        from shapely.ops import unary_union

        polys = [Polygon(b["coords"]) for b in buildings if len(b["coords"]) >= 3]
        if not polys:
            return buildings[0]["coords"]
        merged = unary_union(polys)
        hull = merged.convex_hull
        if hull.geom_type == "Polygon":
            return list(hull.exterior.coords)
        # MultiPolygon (disconnected buildings far apart) — return largest hull
        largest = max(hull.geoms, key=lambda g: g.area)
        return list(largest.exterior.coords)
    except ImportError:
        # No Shapely — return the building with the most vertices (largest)
        return max(buildings, key=lambda b: len(b["coords"]))["coords"]


def match_to_buildings(props: list, buildings: list) -> tuple[dict, list]:
    """
    Match Upland properties to OSM buildings by address.

    A single Upland property may correspond to multiple OSM building footprints
    (e.g. main house + garage + shed on the same lot).  All matching buildings
    are merged into one convex-hull polygon so the map shows one outline per
    property, matching the game's parcel view.

    Returns:
        matched:         {prop_id: {"coords": [...], "prop": p, "osm_ids": [...]}}
        unmatched_props: [prop, ...]
    """
    # Index ALL buildings by (house_num, street) — multiple buildings may share
    # the same address (units A, B, C on the same lot)
    from collections import defaultdict
    bldg_by_key: dict[tuple, list] = defaultdict(list)
    for b in buildings:
        if b["house_num"] and b["street"]:
            key = (b["house_num"].strip(), b["street"].strip())
            bldg_by_key[key].append(b)

    def _num_base(n: str) -> str:
        return n.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")

    def _street_match(a: str, b: str) -> bool:
        return a == b or a in b or b in a

    # prop_id → list of matching buildings
    prop_buildings: dict = defaultdict(list)
    prop_map: dict = {}

    for prop in props:
        raw = prop.get("address", "")
        num, street = _normalize(raw)
        num_base = _num_base(num)
        pid = prop.get("id", raw)

        found: list = []
        if num and street:
            # 1. Exact match
            found = bldg_by_key.get((num, street), [])

            # 2. Fuzzy street, exact number
            if not found:
                for (bnum, bstreet), blist in bldg_by_key.items():
                    if bnum == num and _street_match(street, bstreet):
                        found = blist
                        break

            # 3. Base-number match — collect ALL units (45A, 45B, 45C → "45")
            if not found and num_base:
                for (bnum, bstreet), blist in bldg_by_key.items():
                    if _num_base(bnum) == num_base and _street_match(street, bstreet):
                        found = found + blist  # accumulate all unit buildings

        if found:
            prop_buildings[pid].extend(found)
            prop_map[pid] = prop

    matched: dict = {}
    for pid, blist in prop_buildings.items():
        prop = prop_map[pid]
        # Deduplicate buildings by osm_id
        seen = {}
        for b in blist:
            seen[b["osm_id"]] = b
        unique_bldgs = list(seen.values())
        merged_coords = _merge_building_coords(unique_bldgs)
        matched[pid] = {
            "coords": merged_coords,
            "prop": prop,
            "osm_ids": list(seen.keys()),
            "building_count": len(unique_bldgs),
        }

    matched_pids = set(matched.keys())
    unmatched = [p for p in props if p.get("id", p.get("address")) not in matched_pids]

    multi = sum(1 for v in matched.values() if v["building_count"] > 1)
    pct = len(matched) / max(len(props), 1) * 100
    print(f"[+] Matched {len(matched)}/{len(props)} properties to OSM buildings ({pct:.0f}%)"
          + (f"  [{multi} merged from multiple footprints]" if multi else ""))
    return matched, unmatched

# ─────────────────────────────────────────────────────────────────────────────
# HTML interactive map (folium)
# ─────────────────────────────────────────────────────────────────────────────

def _prop_color(prop: dict, user_prop_ids: set) -> str:
    """Return the display color for a property, gold if owned by tracked user."""
    if str(prop.get("id", "")) in user_prop_ids:
        return USER_COLOR
    return STATUS_COLORS.get(prop.get("status", "Unknown"), DEFAULT_COLOR)


def _popup_html(prop: dict, structures: list, is_user_prop: bool = False) -> str:
    """
    structures: list of {"buildingName": ..., "buildingType": ..., "constructionStatus": ...}
    """
    status = prop.get("status", "Unknown")
    color = USER_COLOR if is_user_prop else STATUS_COLORS.get(status, DEFAULT_COLOR)
    collection = prop.get("collection") or {}

    badge = (
        f'<span style="background:{USER_COLOR};color:white;padding:1px 8px;'
        f'border-radius:10px;font-size:11px;margin-left:8px">YOUR PROPERTY</span>'
        if is_user_prop else ""
    )

    rows = [
        ("Address",    prop.get("address", "N/A")),
        ("Status",     f'<span style="color:{color};font-weight:bold">{status}</span>'),
        ("Mint Price", f"{prop.get('mintPrice', 'N/A')} UPX"),
    ]
    if collection.get("name"):
        rows.append(("Collection", collection["name"]))

    if structures:
        struct_lines = []
        for s in structures:
            name = s.get("buildingName", "?")
            btype = s.get("buildingType", "")
            cstatus = s.get("constructionStatus", "")
            label = name
            if cstatus and cstatus != "completed":
                label += f" <i>({cstatus})</i>"
            if btype:
                label += f" <span style='color:#999;font-size:11px'>({btype})</span>"
            struct_lines.append(label)
        rows.append(("Structures", "<br>".join(struct_lines)))
    else:
        rows.append(("Structures", '<span style="color:#bbb">None</span>'))

    table = "".join(
        f"<tr>"
        f"<td style='color:#666;padding:3px 10px 3px 0;white-space:nowrap;vertical-align:top'>{k}</td>"
        f"<td style='padding:3px 0'>{v}</td>"
        f"</tr>"
        for k, v in rows
    )
    return (
        f'<div style="font-family:Arial,sans-serif;font-size:13px;min-width:220px">'
        f'<b style="font-size:14px">{prop.get("address","")}</b>{badge}'
        f'<table style="border-collapse:collapse;margin-top:6px;width:100%">{table}</table>'
        f'<div style="margin-top:6px;color:#aaa;font-size:11px">Upland ID: {prop.get("id","")}</div>'
        f"</div>"
    )


def _legend_html(username: str = "", statuses_present: set = None) -> str:
    show = statuses_present if statuses_present is not None else set(STATUS_COLORS.keys())
    items = "".join(
        f'<div style="margin:4px 0">'
        f'<span style="display:inline-block;width:14px;height:14px;'
        f'background:{color};border-radius:2px;margin-right:8px;vertical-align:middle"></span>'
        f'{status}'
        f'</div>'
        for status, color in STATUS_COLORS.items()
        if status in show
    )
    user_row = ""
    if username:
        user_row = (
            f'<div style="margin-top:8px;padding-top:8px;border-top:1px solid #eee">'
            f'<span style="display:inline-block;width:14px;height:14px;'
            f'background:{USER_COLOR};border-radius:2px;margin-right:8px;vertical-align:middle"></span>'
            f'<b>{username}</b> (your properties)'
            f'</div>'
        )
    return (
        '<div style="position:fixed;bottom:30px;right:12px;background:white;padding:12px 16px;'
        'border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.3);z-index:1000;'
        'font-family:Arial,sans-serif;font-size:13px">'
        '<b style="display:block;margin-bottom:8px">Property Status</b>'
        f'{items}'
        f'{user_row}'
        '<div style="margin-top:8px;padding-top:8px;border-top:1px solid #eee;color:#999">'
        '<span style="margin-right:6px">●</span>'
        'Circle = no building outline in OSM'
        '</div>'
        '</div>'
    )


def render_html_map(
    hood: dict,
    props: list,
    buildings: list,
    matched: dict,
    unmatched_props: list,
    structure_data: dict,
    user_prop_ids: set,
    geocode_map: dict,
    username: str,
    output_path: Path,
    road_polylines: list = None,
    upland_style: bool = False,
) -> None:
    # structure_data: {prop_id_str: [{"buildingName":..., ...}, ...]} from get_upland_property_structures
    try:
        import folium
        from folium import Popup
    except ImportError:
        print("[!] folium not installed. Run: pip install folium")
        return

    center = hood.get("center", [0, 0])  # [lon, lat]

    if upland_style and road_polylines is not None:
        # Tile-free dark Upland-style map
        m = folium.Map(location=[center[1], center[0]], zoom_start=15, tiles=None)
        # Dark background
        m.get_root().html.add_child(folium.Element(
            '<style>.leaflet-container{background:#1a1a2e!important}</style>'
        ))
        # Road geometry as thin dark-gray lines
        for line in road_polylines:
            folium.PolyLine(
                locations=line,
                color="#444466",
                weight=1.5,
                opacity=0.8,
            ).add_to(m)
    else:
        # CartoDB Positron: clean base map with no POI clutter
        m = folium.Map(location=[center[1], center[0]], zoom_start=15,
                       tiles="CartoDB positron")

    # Neighborhood boundary
    boundary = hood.get("boundaries")
    if boundary:
        ring = boundary[0] if isinstance(boundary[0][0], (list, tuple)) else boundary
        folium_ring = [[pt[1], pt[0]] for pt in ring]
        folium.Polygon(
            locations=folium_ring,
            color="#2C3E50",
            weight=3,
            fill=False,
            tooltip=f"Neighborhood: {hood['name']}",
            popup=Popup(
                f"<b>{hood['name']}</b><br>"
                f"City: {hood.get('city_name','')}<br>"
                f"Area: {hood.get('area', 0)/1e6:.2f} km²<br>"
                f"Upland properties: {len(props)}<br>"
                f"With OSM outlines: {len(matched)}",
                max_width=200,
            ),
        ).add_to(m)

    # Matched properties — one polygon per property (merged if multiple buildings)
    for pid, info in matched.items():
        prop = info["prop"]
        coords = info["coords"]
        is_user = str(prop.get("id", "")) in user_prop_ids
        color = _prop_color(prop, user_prop_ids)
        structs = structure_data.get(str(prop.get("id", "")), [])
        coords_latlon = [[pt[1], pt[0]] for pt in coords]
        struct_names = ", ".join(s["buildingName"] for s in structs if s.get("buildingName"))
        tooltip_text = prop.get("address", "")
        if struct_names:
            tooltip_text += f" — {struct_names}"
        folium.Polygon(
            locations=coords_latlon,
            color=color,
            weight=2 if is_user else 1,
            fill=True,
            fill_color=color,
            fill_opacity=0.75 if is_user else 0.65,
            popup=Popup(_popup_html(prop, structs, is_user), max_width=340),
            tooltip=tooltip_text,
        ).add_to(m)
        # Add a small marker dot on properties with structures
        if structs:
            centroid = coords_latlon[len(coords_latlon) // 2]
            cx = sum(c[0] for c in coords_latlon) / len(coords_latlon)
            cy = sum(c[1] for c in coords_latlon) / len(coords_latlon)
            folium.CircleMarker(
                location=[cx, cy],
                radius=3,
                color="white",
                weight=1,
                fill=True,
                fill_color="white",
                fill_opacity=0.9,
                tooltip=struct_names,
            ).add_to(m)

    # Unmatched Upland properties — geocoded circle markers
    for prop in unmatched_props:
        addr_key = prop["address"].upper().strip()
        coords = geocode_map.get(addr_key)
        if not coords:
            continue
        lat, lon = coords[0], coords[1]
        is_user = str(prop.get("id", "")) in user_prop_ids
        color = _prop_color(prop, user_prop_ids)
        structs = structure_data.get(str(prop.get("id", "")), [])
        struct_names = ", ".join(s["buildingName"] for s in structs if s.get("buildingName"))
        folium.CircleMarker(
            location=[lat, lon],
            radius=6,
            color=color,
            weight=2 if is_user else 1,
            fill=True,
            fill_color=color,
            fill_opacity=0.75,
            popup=Popup(_popup_html(prop, structs, is_user), max_width=340),
            tooltip=f"{prop.get('address','')} — {struct_names or prop.get('status','')}",
        ).add_to(m)

    geocoded_count = sum(1 for p in unmatched_props
                         if geocode_map.get(p["address"].upper().strip()))
    total_shown = len(matched) + geocoded_count

    title = (
        f'<div style="position:fixed;top:10px;left:50%;transform:translateX(-50%);'
        f'background:white;padding:10px 20px;border-radius:8px;'
        f'box-shadow:0 2px 8px rgba(0,0,0,.3);z-index:1000;font-family:Arial,sans-serif">'
        f'<b style="font-size:16px">🏠 {hood["name"]}</b>'
        f'<span style="color:#666;margin-left:12px;font-size:13px">'
        f'{len(props)} properties • {total_shown} on map'
        + (f' • <b style="color:{USER_COLOR}">{username}</b>' if username else '')
        + f'</span></div>'
    )
    statuses_present = {p.get("status", "Unknown") for p in props}
    m.get_root().html.add_child(folium.Element(title))
    m.get_root().html.add_child(folium.Element(_legend_html(username, statuses_present)))

    m.save(str(output_path))
    print(f"[+] Saved interactive map → {output_path}")

# ─────────────────────────────────────────────────────────────────────────────
# Static PNG (matplotlib)
# ─────────────────────────────────────────────────────────────────────────────

def render_png_map(
    hood: dict,
    props: list,
    buildings: list,
    matched: dict,
    unmatched_props: list,
    structure_data: dict,
    user_prop_ids: set,
    geocode_map: dict,
    username: str,
    output_path: Path,
) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
    except ImportError:
        print("[!] matplotlib not installed. Run: pip install matplotlib")
        return

    fig, ax = plt.subplots(figsize=(14, 14))
    ax.set_aspect("equal")

    boundary = hood.get("boundaries")
    min_lon, max_lon, min_lat, max_lat = 180, -180, 90, -90

    if boundary:
        ring = boundary[0] if isinstance(boundary[0][0], (list, tuple)) else boundary
        lons = [pt[0] for pt in ring]
        lats = [pt[1] for pt in ring]
        min_lon, max_lon = min(lons), max(lons)
        min_lat, max_lat = min(lats), max(lats)

        # CartoDB Positron tile background (clean, no POI icons)
        try:
            import contextily as ctx
            pad_lon = (max_lon - min_lon) * 0.08
            pad_lat = (max_lat - min_lat) * 0.08
            ax.set_xlim(min_lon - pad_lon, max_lon + pad_lon)
            ax.set_ylim(min_lat - pad_lat, max_lat + pad_lat)
            ctx.add_basemap(ax, crs="EPSG:4326",
                            source=ctx.providers.CartoDB.Positron)
        except Exception:
            pass

    # Matched properties — one polygon per property (merged if multiple buildings)
    for pid, info in matched.items():
        prop = info["prop"]
        coords = info["coords"]
        color = _prop_color(prop, user_prop_ids)
        is_user = str(prop.get("id", "")) in user_prop_ids
        lons_b = [pt[0] for pt in coords]
        lats_b = [pt[1] for pt in coords]
        ax.fill(lons_b, lats_b, color=color, alpha=0.8 if is_user else 0.7, zorder=3)
        ax.plot(lons_b, lats_b, color=color, linewidth=0.8 if is_user else 0.4, zorder=3)
        if structure_data.get(str(prop.get("id", ""))):
            cx = sum(lons_b) / len(lons_b)
            cy = sum(lats_b) / len(lats_b)
            ax.plot(cx, cy, "w*", markersize=5, zorder=4)
        for lon in lons_b:
            min_lon, max_lon = min(min_lon, lon), max(max_lon, lon)
        for lat in lats_b:
            min_lat, max_lat = min(min_lat, lat), max(max_lat, lat)

    # Unmatched properties — geocoded circle dots
    for prop in unmatched_props:
        coords = geocode_map.get(prop["address"].upper().strip())
        if not coords:
            continue
        lat, lon = coords[0], coords[1]
        color = _prop_color(prop, user_prop_ids)
        is_user = str(prop.get("id", "")) in user_prop_ids
        ax.plot(lon, lat, "o", color=color,
                markersize=5 if is_user else 4,
                markeredgewidth=0.5, markeredgecolor="white", zorder=3)
        min_lon, max_lon = min(min_lon, lon), max(max_lon, lon)
        min_lat, max_lat = min(min_lat, lat), max(max_lat, lat)

    # Neighborhood boundary on top
    if boundary:
        ring = boundary[0] if isinstance(boundary[0][0], (list, tuple)) else boundary
        lons = [pt[0] for pt in ring]
        lats = [pt[1] for pt in ring]
        ax.plot(lons, lats, color="#2C3E50", linewidth=2.0, zorder=5)

    pad_lon = (max_lon - min_lon) * 0.05 or 0.002
    pad_lat = (max_lat - min_lat) * 0.05 or 0.002
    ax.set_xlim(min_lon - pad_lon, max_lon + pad_lon)
    ax.set_ylim(min_lat - pad_lat, max_lat + pad_lat)

    legend_patches = [
        mpatches.Patch(color=color, label=status)
        for status, color in STATUS_COLORS.items()
    ]
    if username:
        legend_patches.append(
            mpatches.Patch(color=USER_COLOR, label=f"{username} (yours)")
        )
    ax.legend(handles=legend_patches, loc="upper right", fontsize=9, framealpha=0.95)

    geocoded_count = sum(1 for p in unmatched_props
                         if geocode_map.get(p["address"].upper().strip()))
    ax.set_title(
        f"{hood['name']}  ({hood.get('city_name', '')})\n"
        f"{len(props)} Upland properties  •  "
        f"{len(matched)} outlines  •  {geocoded_count} geocoded dots  •  "
        f"★ = game structure",
        fontsize=12, pad=10,
    )
    ax.set_xlabel("Longitude", fontsize=9)
    ax.set_ylabel("Latitude", fontsize=9)
    ax.tick_params(labelsize=7)

    plt.tight_layout()
    plt.savefig(str(output_path), dpi=150, bbox_inches="tight")
    plt.close()
    print(f"[+] Saved static image    → {output_path}")

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate an Upland neighborhood map with property outlines",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 neighborhood_map.py "Inner Richmond"
  python3 neighborhood_map.py "Tenderloin" --city "San Francisco"
  python3 neighborhood_map.py "Lincoln Square" --city "Chicago"
  python3 neighborhood_map.py --list-neighborhoods
  python3 neighborhood_map.py --list-neighborhoods --city "Chicago"
  python3 neighborhood_map.py "Tenderloin" --html-only
  python3 neighborhood_map.py "Tenderloin" --refresh-cache
  python3 neighborhood_map.py "Tenderloin" --output-dir ~/Desktop/maps
        """,
    )
    parser.add_argument("neighborhood", nargs="?", help="Neighborhood name to map")
    parser.add_argument("--city", help="City hint to narrow neighborhood search")
    parser.add_argument("--output-dir", default=".", help="Directory for output files (default: .)")
    parser.add_argument("--list-neighborhoods", action="store_true",
                        help="List all available neighborhoods and exit")
    parser.add_argument("--no-buildings", action="store_true",
                        help="Skip OSM building footprint lookup (faster, no outlines)")
    parser.add_argument("--html-only", action="store_true",
                        help="Only generate the HTML map (skip PNG)")
    parser.add_argument("--refresh-cache", action="store_true",
                        help="Ignore cached property data and re-fetch from API")
    parser.add_argument("--username", default=DEFAULT_USERNAME,
                        help=f"Upland username to highlight in gold (default: {DEFAULT_USERNAME})")
    parser.add_argument("--no-username", action="store_true",
                        help="Disable username highlighting")
    parser.add_argument("--eos-account", default=DEFAULT_EOS_ACCOUNT,
                        help=f"EOS blockchain account for --username (default: {DEFAULT_EOS_ACCOUNT}). "
                             "Required for gold highlighting via blockchain lookup.")
    parser.add_argument("--user-props-file", type=Path,
                        help="JSON file with array of property IDs owned by --username")
    parser.add_argument("--no-geocode", action="store_true",
                        help="Skip geocoding of unmatched properties (faster)")
    parser.add_argument("--upland-style", action="store_true",
                        help="Render HTML map in dark Upland-style (no tile server, road lines from OSM)")
    args = parser.parse_args()

    # ── List mode ──────────────────────────────────────────────────────────────
    if args.list_neighborhoods:
        print("[*] Fetching all neighborhoods...")
        hoods = list_all_neighborhoods(city_filter=args.city)
        by_city: dict[str, list] = {}
        for h in hoods:
            by_city.setdefault(h["city_name"], []).append(h)
        for city_name, city_hoods in sorted(by_city.items()):
            print(f"\n{city_name}:")
            for h in sorted(city_hoods, key=lambda x: x["name"]):
                area_km2 = h.get("area", 0) / 1e6
                print(f"  [{h['id']:>6}]  {h['name']:<35}  {area_km2:.2f} km²")
        return

    if not args.neighborhood:
        parser.print_help()
        sys.exit(1)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    safe_name = "".join(
        c if c.isalnum() or c in " -_" else "_" for c in args.neighborhood
    ).strip().replace(" ", "_")

    html_path      = output_dir / f"{safe_name}.html"
    png_path       = output_dir / f"{safe_name}.png"
    cache_path     = output_dir / f"{safe_name}_props_cache.json"
    geocode_cache  = output_dir / f"{safe_name}_geocode_cache.json"
    pluto_cache    = output_dir / f"{safe_name}_pluto_cache.json"

    if args.refresh_cache and cache_path.exists():
        cache_path.unlink()
        print(f"[*] Cleared property cache: {cache_path.name}")

    # ── Step 1: Find neighborhood ─────────────────────────────────────────────
    print(f"\n{'='*55}")
    print(f"  Upland Neighborhood Map: {args.neighborhood}")
    print(f"{'='*55}\n")

    try:
        hood = find_neighborhood(args.neighborhood, city_hint=args.city)
    except ValueError as e:
        print(f"[!] {e}")
        sys.exit(1)

    boundary = hood.get("boundaries")
    center   = hood.get("center", [0, 0])

    print(f"    City:   {hood.get('city_name', 'N/A')}")
    print(f"    Area:   {hood.get('area', 0)/1e6:.2f} km²")
    print(f"    Center: {center[1]:.5f}, {center[0]:.5f}")
    print(f"    Has boundary: {'yes' if boundary else 'NO — map will be limited'}")

    if not boundary:
        print("[!] No boundary polygon returned by the API. Cannot draw neighborhood outline.")
        print("    The map will show only markers.")

    # ── Step 2: Fetch Upland properties ──────────────────────────────────────
    props = get_neighborhood_properties(
        city_id=hood["city_id"],
        neighborhood_id=hood["id"],
        neighborhood_name=hood["name"],
        cache_path=cache_path,
        boundary_coords=boundary,
    )

    if not props:
        print("[!] No properties found for this neighborhood.")
        sys.exit(1)

    # Status breakdown
    from collections import Counter
    status_counts = Counter(p.get("status", "Unknown") for p in props)
    print("\n[i] Property status breakdown:")
    for status, count in status_counts.most_common():
        color_dot = "●"
        print(f"    {color_dot} {status:<20} {count:>4} properties")

    # ── Step 3: Property outlines — MapPLUTO (NYC) or OSM buildings ──────────
    buildings: list = []
    addr_nodes: list = []
    city_name = hood.get("city_name", "")

    if args.no_buildings:
        pass  # skip — handled below
    elif not boundary:
        pass  # no boundary — handled below
    elif _is_nyc(city_name):
        # NYC: use MapPLUTO tax lot parcels — exact match to Upland property outlines
        buildings = get_nyc_pluto_parcels(boundary, cache_path=pluto_cache)
        if not buildings:
            print("[~] MapPLUTO returned nothing — falling back to OSM buildings")
            buildings, addr_nodes = get_overpass_buildings(boundary)
    else:
        buildings, addr_nodes = get_overpass_buildings(boundary)

    if args.no_buildings:
        print("[*] Skipping OSM buildings (--no-buildings)")
    else:
        print("[!] No boundary — skipping OSM buildings lookup")

    # ── Step 4: Match properties to buildings ────────────────────────────────
    if buildings:
        matched, unmatched_props = match_to_buildings(props, buildings)
    else:
        matched, unmatched_props = {}, props

    # ── Step 5: Upland game structures ───────────────────────────────────────
    struct_cache = output_dir / f"{safe_name}_structures_cache.json"
    structure_data: dict = get_upland_property_structures(props, struct_cache)

    # ── Step 6: Username / user-owned properties ──────────────────────────────
    username = "" if args.no_username else args.username
    user_prop_ids: set = set()
    if username:
        bc_cache = _SCRIPT_DIR / f"{username}_blockchain_cache.json"
        eos_account = "" if args.no_username else args.eos_account
        user_prop_ids = get_user_property_ids(
            hood["city_id"], username,
            eos_account=eos_account,
            user_props_file=args.user_props_file,
            blockchain_cache=bc_cache,
        )
        if user_prop_ids:
            in_hood = sum(1 for p in props if str(p.get("id","")) in user_prop_ids)
            print(f"[+] Highlighting {in_hood}/{len(props)} props for '{username}' "
                  f"({len(user_prop_ids)} total portfolio)")

    # ── Step 7: Place every unmatched property on the map ────────────────────
    # Priority 1: OSM address nodes we already fetched (free, instant)
    # Priority 2: Nominatim geocoding for anything still missing
    geocode_map: dict = {}

    if unmatched_props:
        # Seed from OSM addr_nodes (same data we already have)
        osm_node_coords = _addr_nodes_to_geocode_map(addr_nodes)
        for prop in unmatched_props:
            key = prop["address"].upper().strip()
            if key in osm_node_coords:
                geocode_map[key] = osm_node_coords[key]

        osm_hits = sum(1 for p in unmatched_props
                       if p["address"].upper().strip() in geocode_map)
        print(f"[+] OSM addr nodes resolved {osm_hits}/{len(unmatched_props)} unmatched properties")

        # Nominatim for the rest (cached after first run)
        still_missing = [p for p in unmatched_props
                         if p["address"].upper().strip() not in geocode_map]
        if still_missing and not args.no_geocode:
            nominatim_map = geocode_props(
                still_missing, hood.get("city_name", ""), geocode_cache
            )
            geocode_map.update(nominatim_map)
        elif still_missing and args.no_geocode:
            print(f"[~] {len(still_missing)} properties still missing coords "
                  f"(skipping Nominatim — remove --no-geocode to resolve them)")
        else:
            print(f"[+] All unmatched properties resolved via OSM nodes")

    # ── Step 7b: OSM road geometry (for --upland-style) ───────────────────────
    road_polylines: list = []
    if args.upland_style and boundary:
        print("[*] Fetching OSM road geometry for dark-style map...")
        road_polylines = get_osm_road_geometry(boundary)

    # ── Step 8: Render maps ───────────────────────────────────────────────────
    print("\n[*] Rendering maps...")

    render_html_map(
        hood=hood,
        props=props,
        buildings=buildings,
        matched=matched,
        unmatched_props=unmatched_props,
        structure_data=structure_data,
        user_prop_ids=user_prop_ids,
        geocode_map=geocode_map,
        username=username,
        output_path=html_path,
        road_polylines=road_polylines if args.upland_style else None,
        upland_style=args.upland_style,
    )

    if not args.html_only:
        render_png_map(
            hood=hood,
            props=props,
            buildings=buildings,
            matched=matched,
            unmatched_props=unmatched_props,
            structure_data=structure_data,
            user_prop_ids=user_prop_ids,
            geocode_map=geocode_map,
            username=username,
            output_path=png_path,
        )

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'='*55}")
    print(f"  Done!")
    print(f"{'='*55}")
    print(f"  Neighborhood:     {hood['name']} ({hood.get('city_name','')})")
    geocoded_count = sum(1 for p in unmatched_props
                         if geocode_map.get(p["address"].upper().strip()))
    has_structs = sum(1 for v in structure_data.values() if v)
    print(f"  Upland props:     {len(props)}")
    print(f"  With outlines:    {len(matched)}")
    print(f"  Geocoded dots:    {geocoded_count} / {len(unmatched_props)} unmatched")
    print(f"  On map total:     {len(matched) + geocoded_count}")
    print(f"  Game structures:  {has_structs} properties")
    if username:
        highlighted_here = sum(1 for p in props if str(p.get("id", "")) in user_prop_ids)
        total_portfolio = len(user_prop_ids)
        print(f"  User '{username}': {highlighted_here} highlighted here "
              f"({total_portfolio} total portfolio)")
    print(f"\n  Output files:")
    print(f"    HTML (interactive): {html_path.resolve()}")
    if not args.html_only:
        print(f"    PNG  (static):      {png_path.resolve()}")
    print()


if __name__ == "__main__":
    main()
