"""
UplandScope — Collection Optimizer

Analyzes a player's property portfolio and determines the optimal assignment
of properties to collections to maximize UPX yield earnings.

The optimization problem:
  - Each property can be in at most ONE collection
  - Collections require N properties matching criteria (street, neighborhood, city)
  - Each collection has a yieldBoost multiplier applied to mintPrice
  - Goal: maximize sum of (mintPrice × yieldBoost) across all completed collections

Algorithm: Greedy heuristic — score each completable collection by total
potential yield gain, fill highest-value first, then iterate.
"""

import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / "cache"
NEIGHBORHOOD_MAP_DIR = SCRIPT_DIR.parent / "optimizer"

sys.path.insert(0, str(NEIGHBORHOOD_MAP_DIR))

RARITY_NAMES = {1: "Standard", 2: "Limited", 3: "Exclusive", 4: "Rare", 5: "Ultra Rare"}

# City ID -> name mapping (loaded lazily from neighborhoods cache)
_CITY_MAP = None

def _city_id_to_name(city_id: int) -> str:
    global _CITY_MAP
    if _CITY_MAP is None:
        _CITY_MAP = {}
        hood_list = CACHE_DIR / "neighborhoods_all.json"
        if hood_list.exists():
            with open(hood_list) as f:
                for h in json.load(f):
                    _CITY_MAP[h["city_id"]] = h["city_name"]
    return _CITY_MAP.get(city_id, "")

# ─────────────────────────────────────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────────────────────────────────────

def load_collections(cache_path: Path = None) -> list[dict]:
    """Load all collections from cache or API."""
    if cache_path is None:
        cache_path = CACHE_DIR / "collections_all.json"

    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < 86400 * 7:  # 7-day TTL
            with open(cache_path) as f:
                return json.load(f)

    # Fetch from API
    from neighborhood_map import _auth_headers, UPLAND_API
    import requests
    print("[*] Fetching collections from API...")
    r = requests.get(f"{UPLAND_API}/collections", headers=_auth_headers(), timeout=30)
    r.raise_for_status()
    data = r.json()
    colls = data.get("results", data) if isinstance(data, dict) else data
    with open(cache_path, "w") as f:
        json.dump(colls, f)
    print(f"[+] Cached {len(colls)} collections")
    return colls


def load_user_properties(username: str, eos_account: str) -> list[dict]:
    """
    Load user's properties with addresses from blockchain + property cache.
    Returns list of {id, address, neighborhood, city, mintPrice}.
    """
    from neighborhood_map import _blockchain_user_properties

    bc_cache = CACHE_DIR / "neighborhoods" / f"{username.lower()}_blockchain_cache.json"
    owned_ids = _blockchain_user_properties(username, eos_account, bc_cache)

    if not owned_ids:
        return []

    # Load the main property cache to get addresses
    main_cache = _load_main_property_cache()

    props = []
    missing_ids = []
    for pid in owned_ids:
        full_addr = main_cache.get(str(pid)) if main_cache else None
        if not full_addr:
            missing_ids.append(str(pid))
            continue
        parts = [p.strip() for p in full_addr.split(",")]
        city = parts[-1] if len(parts) >= 2 else ""
        neighborhood = parts[-2] if len(parts) >= 3 else (parts[-1] if len(parts) == 2 else "")
        address = ", ".join(parts[:-2]) if len(parts) >= 3 else (parts[0] if parts else "")
        props.append({
            "id": str(pid),
            "address": address,
            "neighborhood": neighborhood.upper(),
            "city": city.strip(),
            "mintPrice": 0,
        })

    # Fetch any properties not in the local cache from the Upland API
    if missing_ids:
        print(f"[*] Fetching {len(missing_ids)} properties not in local cache from API...")
        fetched = _fetch_properties_from_api(missing_ids)
        props.extend(fetched)
        # Update in-memory cache so _enrich_mint_prices skips re-fetching these
        if main_cache is not None:
            for p in fetched:
                addr_str = f"{p['address']}, {p['neighborhood']}, {p['city']}"
                main_cache[p['id']] = addr_str

    # Try to enrich with mintPrice from neighborhood caches / mint cache / API
    _enrich_mint_prices(props)

    return props


def _fetch_properties_from_api(prop_ids: list[str]) -> list[dict]:
    """Fetch address + mintPrice for property IDs not in the local cache."""
    from neighborhood_map import _auth_headers, UPLAND_API
    import requests
    import concurrent.futures
    import threading

    results = []
    lock = threading.Lock()
    done = 0

    def fetch_one(pid):
        try:
            r = requests.get(
                f"{UPLAND_API}/properties/{pid}",
                headers=_auth_headers(),
                timeout=15,
            )
            if r.status_code == 200:
                data = r.json()
                addr = data.get("address", "")
                city_obj = data.get("city") or {}
                hood_obj = data.get("neighborhood") or {}
                city = city_obj.get("name", "") if isinstance(city_obj, dict) else str(city_obj)
                neighborhood = hood_obj.get("name", "") if isinstance(hood_obj, dict) else str(hood_obj)
                mint = data.get("mintPrice", 0)
                if addr or neighborhood:
                    return {
                        "id": str(pid),
                        "address": addr.strip(),
                        "neighborhood": neighborhood.upper().strip(),
                        "city": city.strip(),
                        "mintPrice": mint or 0,
                    }
        except Exception:
            pass
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_one, pid): pid for pid in prop_ids}
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            with lock:
                done += 1
                if done % 50 == 0:
                    print(f"  [{done}/{len(prop_ids)}]", end="\r", flush=True)
                if result:
                    results.append(result)

    print(f"\n[+] Fetched {len(results)}/{len(prop_ids)} missing property details from API")
    return results


_main_cache = None

def _load_main_property_cache() -> dict:
    global _main_cache
    if _main_cache is not None:
        return _main_cache

    candidates = [
        SCRIPT_DIR.parent / "scraper" / "property_cache.json",
        SCRIPT_DIR.parent / "property_cache.json",
    ]
    for path in candidates:
        if path.exists():
            print(f"[*] Loading property cache from {path.name}...")
            with open(path) as f:
                _main_cache = json.load(f)
            print(f"[+] {len(_main_cache):,} properties loaded")
            return _main_cache

    _main_cache = {}
    return _main_cache


def _enrich_mint_prices(props: list):
    """
    Fill in mintPrice for all properties.
    Priority: neighborhood cache → developers API (batch, cached).
    """
    # 1. Try neighborhood caches first (fast, free)
    hood_cache_dir = CACHE_DIR / "neighborhoods"
    loaded_hoods = {}
    missing = []

    for p in props:
        hood = p["neighborhood"]
        if hood not in loaded_hoods:
            safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in hood).strip().replace(" ", "_")
            cache_path = hood_cache_dir / f"{safe}_props_cache.json"
            if cache_path.exists():
                with open(cache_path) as f:
                    hood_props = json.load(f)
                loaded_hoods[hood] = {str(hp["id"]): hp.get("mintPrice", 0) for hp in hood_props}
            else:
                loaded_hoods[hood] = {}

        mint = loaded_hoods.get(hood, {}).get(p["id"], 0)
        if mint:
            p["mintPrice"] = mint
        else:
            missing.append(p)

    if not missing:
        return

    # 2. Check mint price cache
    mint_cache_path = CACHE_DIR / "mint_prices.json"
    mint_cache = {}
    if mint_cache_path.exists():
        try:
            with open(mint_cache_path) as f:
                mint_cache = json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass

    still_missing = []
    for p in missing:
        cached_mint = mint_cache.get(p["id"])
        if cached_mint:
            p["mintPrice"] = cached_mint
        else:
            still_missing.append(p)

    if not still_missing:
        return

    # 3. Fetch from developers API (single property lookup)
    print(f"[*] Fetching mint prices for {len(still_missing)} properties via API...")
    import threading
    import concurrent.futures

    from neighborhood_map import _auth_headers, UPLAND_API
    import requests

    fetched = 0
    lock = threading.Lock()

    def fetch_mint(prop):
        try:
            r = requests.get(
                f"{UPLAND_API}/properties/{prop['id']}",
                headers=_auth_headers(),
                timeout=15,
            )
            if r.status_code == 200:
                data = r.json()
                return prop["id"], data.get("mintPrice", 0)
        except Exception:
            pass
        return prop["id"], 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_mint, p): p for p in still_missing}
        for future in concurrent.futures.as_completed(futures):
            pid, mint = future.result()
            if mint:
                mint_cache[pid] = mint
                for p in still_missing:
                    if p["id"] == pid:
                        p["mintPrice"] = mint
                        break
            with lock:
                fetched += 1
                if fetched % 50 == 0:
                    print(f"  [{fetched}/{len(still_missing)}]", end="\r", flush=True)

    print(f"[+] Fetched mint prices for {sum(1 for p in still_missing if p.get('mintPrice'))} properties")

    # Save mint cache
    with open(mint_cache_path, "w") as f:
        json.dump(mint_cache, f)


# ─────────────────────────────────────────────────────────────────────────────
# Requirement parsing
# ─────────────────────────────────────────────────────────────────────────────

_STREET_ABBREV = {
    "AVENUE": "AVE", "STREET": "ST", "BOULEVARD": "BLVD",
    "DRIVE": "DR", "ROAD": "RD", "LANE": "LN", "COURT": "CT",
    "PLACE": "PL", "CIRCLE": "CIR", "TERRACE": "TER",
}


def _normalize_street(s: str) -> str:
    s = s.upper().strip().rstrip(".")
    for full, abbr in _STREET_ABBREV.items():
        s = s.replace(full, abbr)
    return s


def parse_collection_requirement(coll: dict) -> dict:
    """
    Parse a collection's requirement string into structured matching criteria.
    Returns {type, amount, street?, neighborhood?, city?, raw}.
    """
    req = coll.get("requirements", "").strip().rstrip(".")
    amount = coll.get("amount", 0)

    # Special cases
    if "same street" in req.lower():
        return {"type": "same_street", "amount": amount}
    if "same city" in req.lower():
        return {"type": "same_city", "amount": amount}
    if req.lower() in ("own any 1 property in upland", "own 1 property in upland"):
        return {"type": "any_one", "amount": 1}

    # Nomad / Traveler — must check BEFORE generic "in" patterns
    req_lower = req.lower()
    if "different" in req_lower and ("cit" in req_lower or "countr" in req_lower):
        region = None
        if "south american" in req_lower:
            region = "south_america"
        elif "european" in req_lower:
            region = "europe"
        elif "international" in req_lower or "countr" in req_lower:
            region = "international"
        elif "west coast" in req_lower or "western" in req_lower:
            region = "west"
        elif "east coast" in req_lower or "eastern" in req_lower:
            region = "east"
        elif "central" in req_lower:
            region = "central"
        return {"type": "nomad", "amount": amount, "region": region}

    # "Own N properties on STREET in CITY"
    m = re.match(r"Own (?:any )?(\d+) [Pp]ropert(?:y|ies) on (.+?) in (.+)", req)
    if m:
        return {
            "type": "street_city",
            "amount": int(m.group(1)),
            "street": _normalize_street(m.group(2)),
            "city": m.group(3).strip().rstrip("."),
        }

    # "Own N properties on STREET"
    m = re.match(r"Own (?:any )?(\d+) [Pp]ropert(?:y|ies) on (.+)", req)
    if m:
        return {
            "type": "street",
            "amount": int(m.group(1)),
            "street": _normalize_street(m.group(2)),
        }

    # "Own N properties in NEIGHBORHOOD in CITY"
    m = re.match(r"Own (?:any )?(\d+) [Pp]ropert(?:y|ies) in (?:the )?(.+?) (?:neighborhood )?in (.+)", req)
    if m:
        return {
            "type": "neighborhood_city",
            "amount": int(m.group(1)),
            "neighborhood": m.group(2).strip().rstrip(".").upper(),
            "city": m.group(3).strip().rstrip("."),
        }

    # "Own N properties in the NEIGHBORHOOD neighborhood"
    m = re.match(r"Own (?:any )?(\d+) [Pp]ropert(?:y|ies) in (?:the )?(.+?) neighborhood", req)
    if m:
        return {
            "type": "neighborhood",
            "amount": int(m.group(1)),
            "neighborhood": m.group(2).strip().rstrip(".").upper(),
        }

    # "Own N properties in the same city"
    m = re.match(r"Own (?:any )?(\d+) [Pp]ropert(?:y|ies) in the same city", req)
    if m:
        return {"type": "same_city", "amount": int(m.group(1))}

    # "Own N properties in the same street"
    m = re.match(r"Own (?:any )?(\d+) [Pp]ropert(?:y|ies) in the same street", req)
    if m:
        return {"type": "same_street", "amount": int(m.group(1))}

    # "Own N properties in PLACE" (city or neighborhood — ambiguous)
    m = re.match(r"Own (?:any )?(\d+) [Pp]ropert(?:y|ies) in (.+)", req)
    if m:
        return {
            "type": "place",
            "amount": int(m.group(1)),
            "place": m.group(2).strip().rstrip("."),
        }

    # Check if this is a known curated collection
    coll_id = coll.get("id")
    if coll_id and _is_curated(coll_id):
        return {"type": "curated", "amount": amount, "collection_id": coll_id}

    return {"type": "unparsed", "amount": amount, "raw": req}


# ─────────────────────────────────────────────────────────────────────────────
# Property-to-collection matching
# ─────────────────────────────────────────────────────────────────────────────

_REGION_CITIES = {
    "west": {"SAN FRANCISCO", "LOS ANGELES", "OAKLAND", "LAS VEGAS", "SEATTLE",
             "BAKERSFIELD", "FRESNO", "STOCKTON", "SANTA CLARA", "SOUTH LAKE TAHOE",
             "PARK CITY", "VANCOUVER", "PORTLAND"},
    "east": {"MANHATTAN", "BROOKLYN", "QUEENS", "BRONX", "STATEN ISLAND",
             "MIAMI", "MIAMI BEACH", "NASHVILLE", "WASHINGTON", "TRENTON",
             "RUTHERFORD", "FREDERICK", "CLEVELAND", "DETROIT", "BIRMINGHAM",
             "ARLINGTON", "NEW ORLEANS"},
    "central": {"CHICAGO", "KANSAS CITY", "DALLAS"},
    "south_america": {"BUENOS AIRES", "SAO PAULO", "RIO DE JANEIRO"},
    "europe": {"LONDON", "PARIS", "BERLIN", "PORTO", "MADRID", "ROME", "SLOUGH",
               "LISBON", "BIRMINGHAM"},
    "international": set(),  # Any 3 different countries — all cities eligible
}


def _get_region_cities(region: str | None) -> set | None:
    if not region or region == "international":
        return None  # All cities eligible
    return _REGION_CITIES.get(region)


_CURATED_CACHE = None

def _load_curated() -> dict:
    global _CURATED_CACHE
    if _CURATED_CACHE is not None:
        return _CURATED_CACHE
    path = CACHE_DIR / "curated_collections.json"
    if path.exists():
        with open(path) as f:
            _CURATED_CACHE = json.load(f)
    else:
        _CURATED_CACHE = {}
    return _CURATED_CACHE


def _is_curated(coll_id: int) -> bool:
    return str(coll_id) in _load_curated()


_CITY_COUNTRIES = {
    "SAN FRANCISCO": "US", "LOS ANGELES": "US", "OAKLAND": "US", "LAS VEGAS": "US",
    "SEATTLE": "US", "BAKERSFIELD": "US", "FRESNO": "US", "STOCKTON": "US",
    "SANTA CLARA": "US", "SOUTH LAKE TAHOE": "US", "PARK CITY": "US",
    "MANHATTAN": "US", "BROOKLYN": "US", "QUEENS": "US", "BRONX": "US",
    "STATEN ISLAND": "US", "MIAMI": "US", "MIAMI BEACH": "US", "NASHVILLE": "US",
    "WASHINGTON": "US", "TRENTON": "US", "RUTHERFORD": "US", "FREDERICK": "US",
    "CLEVELAND": "US", "DETROIT": "US", "CHICAGO": "US", "KANSAS CITY": "US",
    "DALLAS": "US", "NEW ORLEANS": "US", "ARLINGTON": "US",
    "LONDON": "UK", "SLOUGH": "UK", "BIRMINGHAM": "UK",
    "PARIS": "FR", "SAINT DENIS": "FR",
    "BERLIN": "DE",
    "PORTO": "PT", "LISBON": "PT",
    "MADRID": "ES",
    "ROME": "IT",
    "BUENOS AIRES": "AR",
    "SAO PAULO": "BR", "RIO DE JANEIRO": "BR",
    "TOKYO": "JP", "SAKURA": "JP",
    "SINGAPORE": "SG",
    "HONG KONG": "HK",
    "SYDNEY": "AU",
    "VANCOUVER": "CA",
    "BERMUDA": "BM", "SOUTHAMPTON": "BM",
    "LUSAIL": "QA",
}


def _get_country(city: str) -> str:
    return _CITY_COUNTRIES.get(city.upper(), city.upper())


def _pick_different_cities(available: list, required: int) -> list:
    """Pick N properties from N different cities, preferring highest mintPrice."""
    selected = []
    used_cities = set()
    for p in available:
        city = p.get("city", "").upper()
        if city not in used_cities:
            selected.append(p)
            used_cities.add(city)
            if len(selected) >= required:
                break
    return selected if len(selected) >= required else []


def _pick_different_countries(available: list, required: int) -> list:
    """Pick N properties from N different countries, preferring highest mintPrice."""
    selected = []
    used_countries = set()
    for p in available:
        country = _get_country(p.get("city", ""))
        if country not in used_countries:
            selected.append(p)
            used_countries.add(country)
            if len(selected) >= required:
                break
    return selected if len(selected) >= required else []


def _get_street(address: str) -> str:
    """Extract normalized street name from address."""
    parts = address.upper().strip().split(maxsplit=1)
    if len(parts) < 2:
        return address.upper()
    street = parts[1]
    for full, abbr in _STREET_ABBREV.items():
        street = street.replace(full, abbr)
    return street


def find_eligible_properties(props: list, parsed_req: dict, coll_city_id: int = None) -> list[dict]:
    """
    Find which properties from the user's portfolio match a collection's requirement.
    Uses exact matching for neighborhood names to avoid false positives.
    Filters by collection's cityId when available.
    """
    req_type = parsed_req["type"]

    # Optional city filter from collection metadata
    city_name_filter = None
    if coll_city_id:
        city_name_filter = _city_id_to_name(coll_city_id)

    def _city_ok(p):
        if not city_name_filter:
            return True
        return city_name_filter.upper() in p["city"].upper()

    if req_type == "street_city":
        street = parsed_req["street"]
        city = parsed_req["city"].upper()
        return [p for p in props
                if street in _get_street(p["address"]) and city in p["city"].upper()]

    if req_type == "street":
        street = parsed_req["street"]
        return [p for p in props if street in _get_street(p["address"]) and _city_ok(p)]

    if req_type == "neighborhood_city":
        hood = parsed_req["neighborhood"].replace(" NEIGHBORHOOD", "")
        city = parsed_req["city"].upper()
        # Handle city abbreviations
        city_aliases = {"SF": "SAN FRANCISCO", "LA": "LOS ANGELES", "NYC": "NEW YORK",
                        "KC": "KANSAS CITY", "DC": "WASHINGTON", "NOLA": "NEW ORLEANS"}
        city_full = city_aliases.get(city, city)
        return [p for p in props
                if (p["neighborhood"] == hood or hood in p["neighborhood"])
                and (city in p["city"].upper() or city_full in p["city"].upper())]

    if req_type == "neighborhood":
        hood = parsed_req["neighborhood"].replace(" NEIGHBORHOOD", "")
        return [p for p in props
                if (p["neighborhood"] == hood or hood in p["neighborhood"]) and _city_ok(p)]

    if req_type == "place":
        place = parsed_req["place"].upper()
        # Handle city name variations
        city_aliases = {"SF": "SAN FRANCISCO", "LA": "LOS ANGELES"}
        place_full = city_aliases.get(place, place)
        return [p for p in props
                if (p["neighborhood"] == place or p["neighborhood"] == place_full
                    or p["city"].upper() == place or p["city"].upper() == place_full)
                and _city_ok(p)]

    if req_type == "same_city":
        by_city = defaultdict(list)
        for p in props:
            by_city[p["city"].upper()].append(p)
        if by_city:
            return max(by_city.values(), key=len)
        return []

    if req_type == "same_street":
        by_street = defaultdict(list)
        for p in props:
            by_street[_get_street(p["address"])].append(p)
        if by_street:
            return max(by_street.values(), key=len)
        return []

    if req_type == "curated":
        curated = _load_curated()
        coll_id = str(parsed_req.get("collection_id", ""))
        eligible_ids = set(curated.get(coll_id, []))
        return [p for p in props if p["id"] in eligible_ids]

    if req_type == "any_one":
        return props

    if req_type == "nomad":
        region = parsed_req.get("region")
        region_cities = _get_region_cities(region)
        if region_cities:
            return [p for p in props if p["city"].upper() in region_cities]
        return props  # No region filter = all cities eligible

    if req_type == "different_cities":
        # Need properties from different cities — return all, optimizer handles diversity
        return props

    # For "place" type with no city — check against both city and neighborhood
    if req_type == "place":
        place = parsed_req.get("place", "").upper()
        return [p for p in props
                if place in p["neighborhood"] or place in p["city"].upper()]

    return []


# ─────────────────────────────────────────────────────────────────────────────
# Optimizer
# ─────────────────────────────────────────────────────────────────────────────

def optimize_collections(props: list, collections: list, annual_rate: float = 0.1225) -> dict:
    """
    Global optimizer: assign properties to collections to maximize total yield.

    Strategy:
    1. Build candidate list (all fillable collections with eligible properties)
    2. Greedy initial assignment (highest boost first)
    3. Iterative improvement: try unassigning collections and reassigning
       their properties to unlock blocked collections with higher total yield
    4. Repeat until no more improvements found

    Returns dict with assignments, yield stats, etc.
    """
    # ── Step 1: Build candidates ─────────────────────────────────────────
    candidates = []
    for coll in collections:
        parsed = parse_collection_requirement(coll)
        if parsed["type"] == "unparsed":
            continue

        coll_city_id = coll.get("cityId") or coll.get("city_id")
        eligible = find_eligible_properties(props, parsed, coll_city_id=coll_city_id)
        required = coll.get("amount", parsed.get("amount", 0))

        if len(eligible) < required:
            continue

        boost = coll.get("yieldBoost", coll.get("yield_boost", 1.0))

        candidates.append({
            "collection": coll,
            "parsed": parsed,
            "eligible": eligible,
            "eligible_ids": {p["id"] for p in eligible},
            "required": required,
            "boost": boost,
            "coll_id": coll.get("id"),
        })

    # Index properties by ID for fast lookup
    prop_by_id = {p["id"]: p for p in props}

    # ── Step 2: Greedy initial assignment ────────────────────────────────
    # Sort by boost descending — fill highest-boost collections first
    candidates.sort(key=lambda c: (-c["boost"], -c["required"]))

    # State: coll_id -> [prop_ids]
    assignment_map: dict[int, list[str]] = {}
    assigned_ids: set[str] = set()

    def _try_fill(cand, assigned: set) -> list[str] | None:
        """Try to fill a collection. Returns selected prop IDs or None."""
        available = [p for p in cand["eligible"] if p["id"] not in assigned]
        if len(available) < cand["required"]:
            return None
        available.sort(key=lambda p: -(p.get("mintPrice") or 0))
        if cand["parsed"]["type"] in ("nomad", "different_cities"):
            if cand["parsed"].get("region") == "international":
                selected = _pick_different_countries(available, cand["required"])
            else:
                selected = _pick_different_cities(available, cand["required"])
        else:
            selected = available[:cand["required"]]
        if len(selected) < cand["required"]:
            return None
        return [p["id"] for p in selected]

    for cand in candidates:
        selected = _try_fill(cand, assigned_ids)
        if selected:
            assignment_map[cand["coll_id"]] = selected
            assigned_ids.update(selected)

    # ── Step 3: Iterative improvement ────────────────────────────────────
    # For each unfilled collection, check if removing a conflicting filled
    # collection and reassigning would increase total yield.
    def _total_yield(amap: dict) -> float:
        total = 0
        for cid, pids in amap.items():
            cand = cand_by_id[cid]
            mint = sum((prop_by_id.get(pid, {}).get("mintPrice") or 0) for pid in pids)
            total += mint * (cand["boost"] - 1)
        return total

    cand_by_id = {c["coll_id"]: c for c in candidates}
    filled_ids = set(assignment_map.keys())

    improved = True
    iterations = 0
    max_iterations = 50  # Safety limit

    while improved and iterations < max_iterations:
        improved = False
        iterations += 1
        current_yield = _total_yield(assignment_map)

        for cand in candidates:
            if cand["coll_id"] in assignment_map:
                continue  # Already filled

            # Find which filled collections are blocking this one
            needed_ids = cand["eligible_ids"]
            blockers = []
            for filled_cid, filled_pids in assignment_map.items():
                overlap = set(filled_pids) & needed_ids
                if overlap:
                    blockers.append(filled_cid)

            if not blockers:
                # Not blocked by conflicts — just not enough eligible props
                # Try filling with current state
                selected = _try_fill(cand, assigned_ids)
                if selected:
                    assignment_map[cand["coll_id"]] = selected
                    assigned_ids.update(selected)
                    improved = True
                continue

            # Try removing each blocker and see if we can fill both
            # the new collection AND re-fill the blocker with different props
            for blocker_cid in blockers:
                # Temporarily remove blocker
                blocker_pids = assignment_map[blocker_cid]
                test_assigned = assigned_ids - set(blocker_pids)

                # Try filling the new collection
                new_selected = _try_fill(cand, test_assigned)
                if not new_selected:
                    continue

                test_assigned.update(new_selected)

                # Try re-filling the blocker with remaining props
                blocker_cand = cand_by_id[blocker_cid]
                blocker_new = _try_fill(blocker_cand, test_assigned)

                if blocker_new:
                    # Both can be filled — check if total yield improves
                    test_map = dict(assignment_map)
                    test_map[cand["coll_id"]] = new_selected
                    test_map[blocker_cid] = blocker_new
                    test_yield = _total_yield(test_map)

                    if test_yield > current_yield:
                        # Accept the swap
                        assignment_map[cand["coll_id"]] = new_selected
                        assignment_map[blocker_cid] = blocker_new
                        assigned_ids = set()
                        for pids in assignment_map.values():
                            assigned_ids.update(pids)
                        improved = True
                        break
                else:
                    # Blocker can't be re-filled — check if new collection
                    # alone is worth more than the blocker
                    test_map = dict(assignment_map)
                    del test_map[blocker_cid]
                    test_map[cand["coll_id"]] = new_selected
                    test_yield = _total_yield(test_map)

                    if test_yield > current_yield:
                        del assignment_map[blocker_cid]
                        assignment_map[cand["coll_id"]] = new_selected
                        assigned_ids = set()
                        for pids in assignment_map.values():
                            assigned_ids.update(pids)
                        improved = True
                        break

    # ── Step 4: Property-level swaps ────────────────────────────────────
    # For each unfilled collection, try replacing individual properties
    # in blocking collections with cheaper alternatives to free up the
    # contested property for the unfilled collection.
    swap_improved = True
    swap_iters = 0
    while swap_improved and swap_iters < 30:
        swap_improved = False
        swap_iters += 1
        current_yield = _total_yield(assignment_map)

        for cand in candidates:
            if cand["coll_id"] in assignment_map:
                continue

            # Which specific properties do we need that are taken?
            needed = cand["eligible_ids"]
            for filled_cid, filled_pids in list(assignment_map.items()):
                contested = set(filled_pids) & needed
                if not contested:
                    continue

                filled_cand = cand_by_id[filled_cid]

                # For each contested property, try replacing it in the filled collection
                for contested_pid in contested:
                    # Remove contested prop from filled collection
                    remaining = [pid for pid in filled_pids if pid != contested_pid]
                    test_assigned = set()
                    for cid2, pids2 in assignment_map.items():
                        if cid2 != filled_cid:
                            test_assigned.update(pids2)
                    test_assigned.update(remaining)

                    # Find a replacement for the filled collection
                    replacement_available = [p for p in filled_cand["eligible"]
                                             if p["id"] not in test_assigned and p["id"] != contested_pid]
                    if not replacement_available:
                        continue

                    replacement_available.sort(key=lambda p: -(p.get("mintPrice") or 0))
                    replacement = replacement_available[0]
                    new_filled_pids = remaining + [replacement["id"]]

                    # Now try filling the unfilled collection
                    test_assigned.add(replacement["id"])
                    new_selected = _try_fill(cand, test_assigned)
                    if not new_selected:
                        continue

                    # Check if total yield improved
                    test_map = dict(assignment_map)
                    test_map[filled_cid] = new_filled_pids
                    test_map[cand["coll_id"]] = new_selected
                    test_yield = _total_yield(test_map)

                    if test_yield > current_yield:
                        assignment_map[filled_cid] = new_filled_pids
                        assignment_map[cand["coll_id"]] = new_selected
                        assigned_ids = set()
                        for pids in assignment_map.values():
                            assigned_ids.update(pids)
                        swap_improved = True
                        break
                if swap_improved:
                    break
            if swap_improved:
                break

    # ── Step 5: Try filling any remaining gaps ───────────────────────────
    for cand in candidates:
        if cand["coll_id"] in assignment_map:
            continue
        selected = _try_fill(cand, assigned_ids)
        if selected:
            assignment_map[cand["coll_id"]] = selected
            assigned_ids.update(selected)

    # ── Build output assignments ─────────────────────────────────────────
    effective_rate = annual_rate
    assignments = []
    for coll_id, prop_ids in assignment_map.items():
        cand = cand_by_id[coll_id]
        coll = cand["collection"]
        selected = [prop_by_id[pid] for pid in prop_ids if pid in prop_by_id]
        total_mint = round(sum(p.get("mintPrice") or 0 for p in selected))
        # Monthly yield gain from this collection's boost
        yield_gain = round((total_mint * effective_rate * (cand["boost"] - 1)) / 12)

        coll_city = coll.get("cityId") or coll.get("city_id")
        city_name = selected[0].get("city", "") if selected else ""
        if not city_name and coll_city:
            city_name = _city_id_to_name(coll_city)

        assignments.append({
            "collection_id": coll.get("id"),
            "collection_name": coll.get("name"),
            "rarity": RARITY_NAMES.get(coll.get("rarityLevel", coll.get("category", 0)), "Unknown"),
            "yield_boost": cand["boost"],
            "one_time_reward": coll.get("oneTimeReward", coll.get("one_time_reward", 0)),
            "required": cand["required"],
            "city": city_name,
            "_raw_props": selected,
            "properties": [
                {"id": p["id"], "address": p["address"], "mintPrice": p.get("mintPrice", 0), "city": p.get("city", "")}
                for p in selected
            ],
            "total_mint": total_mint,
            "yield_gain": yield_gain,
        })

    # Compute totals
    unassigned = [p for p in props if p["id"] not in assigned_ids]
    total_mint_all = sum(p.get("mintPrice") or 0 for p in props)
    total_mint_assigned = sum(a["total_mint"] for a in assignments)
    total_yield_gain = sum(a["yield_gain"] for a in assignments)

    # Earnings calculation using the provided annual rate (default 12.25%)
    effective_rate = annual_rate

    # Monthly WITHOUT any collections
    base_monthly = (total_mint_all * effective_rate) / 12

    # Monthly WITH optimized collections:
    # Unassigned props earn base, assigned props earn base * yieldBoost
    unassigned_mint = total_mint_all - total_mint_assigned
    boosted_annual = unassigned_mint * effective_rate
    for a in assignments:
        boosted_annual += a["total_mint"] * effective_rate * a["yield_boost"]

    boosted_monthly = boosted_annual / 12

    # Sort assignments: by city, then by yield_gain descending within city
    assignments.sort(key=lambda a: (a.get("city", "ZZZ"), -a["yield_gain"]))

    # Group by city for display — multi-city collections go in "Multi-City"
    by_city = defaultdict(list)
    for a in assignments:
        prop_cities = set(p.get("city", "") for p in a.get("_raw_props", []) if p.get("city"))
        if not prop_cities:
            # Fallback to the assigned city
            prop_cities = {a.get("city", "")}
        if len(prop_cities) > 1:
            by_city["Multi-City"].append(a)
        else:
            city = a.get("city", "Other") or "Global"
            by_city[city].append(a)

    return {
        "assignments": assignments,
        "by_city": dict(by_city),
        "unassigned_count": len(unassigned),
        "total_properties": len(props),
        "assigned_count": len(assigned_ids),
        "collections_filled": len(assignments),
        "total_mint_all": round(total_mint_all),
        "total_mint_assigned": total_mint_assigned,
        "total_yield_gain_upx": total_yield_gain,
        "base_monthly_yield": round(base_monthly),
        "boosted_monthly_yield": round(boosted_monthly),
        "monthly_gain": round(boosted_monthly - base_monthly),
        "monthly_rate_pct": round(effective_rate / 12 * 100, 2),
        # % improvement in monthly yield vs. the unboosted baseline — total_yield_gain and
        # monthly_gain are the same monthly UPX figure computed two ways; comparing it against
        # base_monthly (not total_mint_all) is what makes this a yield-improvement percentage.
        "improvement_pct": round((total_yield_gain / max(base_monthly, 1)) * 100, 1),
        "one_time_rewards_total": sum(a["one_time_reward"] for a in assignments),
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Upland Collection Optimizer")
    parser.add_argument("--username", default="pugs08")
    parser.add_argument("--eos-account", default="vo1dsqp3qmce")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    print(f"[*] Loading collections...")
    colls = load_collections()
    print(f"[+] {len(colls)} collections")

    print(f"[*] Loading {args.username}'s properties...")
    props = load_user_properties(args.username, args.eos_account)
    print(f"[+] {len(props)} properties")

    if not props:
        print("[!] No properties found")
        sys.exit(1)

    print(f"[*] Optimizing...")
    result = optimize_collections(props, colls)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"\n{'='*60}")
        print(f"  Collection Optimizer — {args.username}")
        print(f"{'='*60}")
        print(f"  Properties: {result['total_properties']}")
        print(f"  Assigned to collections: {result['assigned_count']}")
        print(f"  Collections filled: {result['collections_filled']}")
        print(f"  Unassigned: {result['unassigned_count']}")
        print(f"")
        print(f"  Total portfolio mint value: {result['total_mint_all']:,.0f} UPX")
        print(f"  Base monthly yield ({result['monthly_rate_pct']}%): {result['base_monthly_yield']:,.0f} UPX")
        print(f"  Optimized monthly yield: {result['boosted_monthly_yield']:,.0f} UPX")
        print(f"  Monthly gain: +{result['monthly_gain']:,.0f} UPX")
        print(f"  Yield improvement: +{result['improvement_pct']}%")
        print(f"  One-time rewards: {result['one_time_rewards_total']:,.0f} UPX")
        print(f"")
        print(f"  Top Collections:")
        for a in result["assignments"][:20]:
            props_str = ", ".join(p["address"] for p in a["properties"])
            print(f"    [{a['rarity']}] {a['collection_name']} "
                  f"({a['yield_boost']}x, +{a['yield_gain']:,.0f} UPX)")
            print(f"      {props_str}")
