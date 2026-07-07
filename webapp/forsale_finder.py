"""
UplandScope — For-Sale Finder

Given a collection entry (from collection_tracker.analyze_collections),
finds properties currently listed for sale that would qualify for that
collection and returns them with live UPX/USD prices.

Uses:
  - Developers API (upland_get) to find candidates with "For sale" status
  - Public Upland API to fetch actual listing prices
  - Neighborhood property cache when available (fast path)
  - Results cached 30 minutes to limit API hammering
"""

import json
import sys
import time
import threading
import concurrent.futures
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / "cache"
FORSALE_CACHE_DIR = CACHE_DIR / "forsale"
FORSALE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
FORSALE_CACHE_TTL = 1800  # 30 minutes

# Import from neighborhood_map
sys.path.insert(0, str(SCRIPT_DIR.parent / "optimizer"))
from neighborhood_map import upland_get, _search_city_by_street, _street_to_search_term

SUPPORTABLE_TYPES = {"street_city", "street", "neighborhood_city", "neighborhood", "place"}

import requests as _requests


def _public_api_price(prop_id):
    """
    Fetch listing price + currency from the public Upland API.
    Returns (upx_price, usd_price, currency, owner_username).

    `on_market.currency` ("UPX" or "USD") tells you which field `price` actually
    represents — for a USD listing, `price` holds the *fiat* amount, not a UPX
    price (confirmed empirically: a $5 listing returns price=5, not a UPX figure).
    Reading `price` without checking currency first mislabels USD listings as UPX.
    """
    try:
        r = _requests.get(
            f"https://api.upland.me/properties/{prop_id}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            on_market = data.get("on_market") or {}
            currency = on_market.get("currency", "")
            price_upx = data.get("price") if currency == "UPX" else None
            price_usd = data.get("price") if currency == "USD" else None
            owner = data.get("owner_username", "")
            return price_upx, price_usd, currency, owner
    except Exception:
        pass
    return None, None, "", None


def _fetch_prices_batch(candidates):
    """Add price fields to a list of for-sale candidate dicts (in-place)."""
    lock = threading.Lock()

    def enrich(prop):
        upx, usd, currency, owner = _public_api_price(prop["id"])
        with lock:
            prop["price_upx"] = upx
            prop["price_usd"] = usd
            prop["currency"] = currency
            prop["owner_username"] = owner
            mint = prop.get("mint_price")
            prop["markup_pct"] = (
                round((upx - mint) / mint * 100, 1)
                if currency == "UPX" and mint and upx is not None
                else None
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(enrich, candidates))

    return candidates


def _load_neighborhood_cache(hood_name: str) -> list:
    """Load the webapp neighborhood property cache if it has real statuses (not precache placeholders)."""
    safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in hood_name).strip().replace(" ", "_")
    path = CACHE_DIR / "neighborhoods" / f"{safe}_props_cache.json"
    if not path.exists():
        return []
    with open(path) as f:
        props = json.load(f)
    # Only use if statuses are real (not all "Unknown" from precache script)
    known_status = [p for p in props if p.get("status") not in (None, "Unknown")]
    if not known_status:
        return []
    return props


def _get_neighborhood_candidates(parsed_req: dict, city_id: int) -> list:
    """
    Find candidate properties for a neighborhood-based requirement.
    Returns list of {id, address, status, city, neighborhood}.
    """
    req_type = parsed_req["type"]

    # --- Determine the neighborhood name and city ID to query ---
    if req_type in ("neighborhood_city", "neighborhood"):
        hood_name = parsed_req.get("neighborhood", "")
    elif req_type == "place":
        hood_name = parsed_req.get("place", "")
    else:
        return []

    # Fast path: try the webapp neighborhood cache
    cached = _load_neighborhood_cache(hood_name)
    if cached:
        return cached

    # Slow path: scan city from developers API, filter by neighborhood name
    if not city_id:
        return []

    print(f"[forsale] Scanning city {city_id} for neighborhood '{hood_name}'...")
    found = []
    hood_upper = hood_name.upper()
    first = upland_get("/properties", {"cityId": city_id, "currentPage": 1, "pageSize": 100})
    total = first.get("totalResults", 0)
    max_pages = min(30, -(-total // 100))  # cap at 3000 props to avoid slow scans

    for p in first.get("results", []):
        nh = (p.get("neighborhood") or {}).get("name", "").upper()
        if hood_upper in nh or nh in hood_upper:
            found.append(p)

    for page in range(2, max_pages + 1):
        data = upland_get("/properties", {"cityId": city_id, "currentPage": page, "pageSize": 100})
        for p in data.get("results", []):
            nh = (p.get("neighborhood") or {}).get("name", "").upper()
            if hood_upper in nh or nh in hood_upper:
                found.append(p)
        if not data.get("results"):
            break
        time.sleep(0.2)

    return found


def _get_street_candidates(parsed_req: dict, city_id: int) -> list:
    """
    Find candidate properties for a street-based requirement.
    Returns list of {id, address, status, city, neighborhood}.
    """
    if not city_id:
        return []

    street = parsed_req.get("street", "")
    if not street:
        return []

    search_term = _street_to_search_term(street)
    print(f"[forsale] Searching street '{search_term}' in city {city_id}...")
    return _search_city_by_street(city_id, search_term)


def find_forsale_for_collection(coll_entry: dict, user_prop_ids: set) -> list:
    """
    Find for-sale properties that would qualify for a collection.

    Returns list of dicts sorted by UPX price ascending (all qualifying listings
    included, not pre-filtered by currency — the UI filters/sorts client-side):
      {id, address, price_upx, price_usd, currency, markup_pct, mint_price,
       city, neighborhood, owner_username}
    """
    coll_id = coll_entry["id"]
    cache_path = FORSALE_CACHE_DIR / f"coll_{coll_id}.json"

    # Serve from cache if fresh
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < FORSALE_CACHE_TTL:
            with open(cache_path) as f:
                return json.load(f)

    req_type = coll_entry.get("req_type", "")
    parsed_req = coll_entry.get("parsed_req", {})
    city_id = coll_entry.get("city_id")

    if req_type not in SUPPORTABLE_TYPES:
        return []

    # Get candidates
    if req_type in ("street_city", "street"):
        candidates = _get_street_candidates(parsed_req, city_id)
    else:
        candidates = _get_neighborhood_candidates(parsed_req, city_id)

    if not candidates:
        return []

    # Filter: for sale only, not owned by user
    for_sale = [
        p for p in candidates
        if p.get("status") == "For sale"
        and str(p.get("id")) not in user_prop_ids
    ]

    if not for_sale:
        result = []
        with open(cache_path, "w") as f:
            json.dump(result, f)
        return result

    # Normalize shape and fetch prices
    normalized = []
    for p in for_sale[:20]:  # cap at 20 to limit API calls
        city_obj = p.get("city") or {}
        hood_obj = p.get("neighborhood") or {}
        normalized.append({
            "id": p.get("id") or p.get("prop_id"),
            "address": p.get("address") or p.get("full_address", ""),
            "city": city_obj.get("name", "") if isinstance(city_obj, dict) else str(city_obj),
            "neighborhood": hood_obj.get("name", "") if isinstance(hood_obj, dict) else str(hood_obj),
            "price_upx": None,
            "price_usd": None,
            "currency": "",
            "markup_pct": None,
            "owner_username": None,
            "mint_price": p.get("mintPrice"),
        })

    _fetch_prices_batch(normalized)

    # Default sort: UPX price ascending (unlisted/USD-only listings last).
    # Currency filtering and markup sorting are done client-side — all listings
    # are returned here so the UI can re-slice without re-fetching.
    result = sorted(normalized, key=lambda x: (x["price_upx"] is None, x["price_upx"] or 0))

    with open(cache_path, "w") as f:
        json.dump(result, f)

    return result
