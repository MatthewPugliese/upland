#!/usr/bin/env python3
"""
Upland Property Listings Tracker
Monitors the blockchain for property listing/sale events
Uses Upland Developers API for address lookups
"""

import requests
import time
import json
import re
import os
import base64
import signal
import sys
from urllib.parse import quote
from datetime import datetime, timezone

# Graceful exit handling
def signal_handler(sig, frame):
    print("\n\n[!] Interrupted - saving cache...")
    save_cache()
    print(f"[+] Cache saved ({len(property_cache)} properties)")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

# ============================================================
# CONFIGURATION - Set your API credentials here
# ============================================================
UPLAND_APP_ID = os.environ.get("UPLAND_APP_ID", "")
UPLAND_SECRET = os.environ.get("UPLAND_SECRET", "")
UPLAND_API_URL = "https://api.prod.upland.me/developers-api"

# Check if credentials are set
def check_api_credentials():
    if not UPLAND_APP_ID or not UPLAND_SECRET or UPLAND_APP_ID == "YOUR_APP_ID":
        print("[!] API credentials not set!")
        print("    Set environment variables:")
        print("      export UPLAND_APP_ID='your_app_id'")
        print("      export UPLAND_SECRET='your_secret_key'")
        print("    Or edit the script directly.")
        return False
    return True
# ============================================================

CHAIN_URL = "https://chain-history.upland.me"
CACHE_FILE = "property_cache.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
}

# Property ID -> Address cache
property_cache = {}

# Action type mappings
LISTING_ACTIONS = {
    "n2": "LISTED_FOR_SALE",
    "n4": "UNLISTED",
    "n5": "SALE_COMPLETE",
}

PARAM_MAP = {
    "a54": "seller",
    "a45": "property_id",
    "p11": "upx_price",
    "p3": "fiat_price",
    "p14": "buyer",
    "p24": "sale_price",
}

def get_api_auth():
    """Generate headers for Upland API (Basic Auth + User-Agent)"""
    credentials = f"{UPLAND_APP_ID}:{UPLAND_SECRET}"
    encoded = base64.b64encode(credentials.encode()).decode()
    return {
        "Authorization": f"Basic {encoded}",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }

def load_cache():
    global property_cache
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r") as f:
            property_cache = json.load(f)
        print(f"[+] Loaded {len(property_cache)} cached properties")

def save_cache():
    with open(CACHE_FILE, "w") as f:
        json.dump(property_cache, f, indent=2)

def extract_address_from_memo(memo):
    """Extract address from n5 sale memo"""
    match = re.search(r"owns (.+?) on Upland", memo)
    if match:
        return match.group(1)
    return None

def cache_property(property_id, address):
    """Add property to cache"""
    if property_id and address:
        property_cache[str(property_id)] = address

def get_address(property_id):
    """Look up address from cache"""
    return property_cache.get(str(property_id))

# ============================================================
# Upland Developers API Functions
# ============================================================

def api_get_cities():
    """Get list of all cities"""
    r = requests.get(f"{UPLAND_API_URL}/cities", headers=get_api_auth())
    r.raise_for_status()
    return r.json().get("cities", [])

def api_get_neighborhoods(city_id):
    """Get neighborhoods for a city"""
    params = {"cityId": city_id}
    r = requests.get(f"{UPLAND_API_URL}/neighborhoods", headers=get_api_auth(), params=params)
    r.raise_for_status()
    return r.json().get("results", [])

from urllib.parse import quote

def api_get_properties(city_id, page=1, page_size=100, text_search=None, retries=3):
    """Get properties for a city, optionally filtered by text search"""
    page_size = max(10, page_size)  # API minimum is 10
    url = f"{UPLAND_API_URL}/properties?cityId={city_id}&currentPage={page}&pageSize={page_size}"
    
    if text_search:
        encoded_search = quote(text_search, safe='')
        url += f"&textSearch={encoded_search}"
    
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=get_api_auth())
            if r.status_code == 409:
                # Conflict/rate limit - wait and retry
                wait = (attempt + 1) * 2
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            if attempt < retries - 1:
                time.sleep((attempt + 1) * 2)
            else:
                raise e
    
    return {"results": [], "totalResults": 0}

def fetch_properties_with_search(city_id, city_name, text_search=None, label=None, quiet=False):
    """Fetch all properties for a search query, respecting pagination limits"""
    page = 1
    total_fetched = 0
    properties = []

    # API limits: pageSize max 100, pages max 99 (9,900 max per query)
    page_size = 100
    max_pages = 99

    while True:
        try:
            data = api_get_properties(city_id, page=page, page_size=page_size, text_search=text_search)
            results = data.get("results", [])
            total = data.get("totalResults", 0)

            for prop in results:
                prop_id = str(prop.get("id"))
                address = prop.get("address", "")
                neighborhood = prop.get("neighborhood", {}).get("name", "")
                full_address = f"{address}, {city_name}"
                if neighborhood:
                    full_address = f"{address}, {neighborhood}, {city_name}"
                properties.append((prop_id, full_address))

            total_fetched += len(results)

            if not quiet:
                display = label or text_search or "all"
                print(f"    {display}: {total_fetched}/{total}      ", end="\r")

            if total_fetched >= total or len(results) == 0 or page >= max_pages:
                break

            page += 1
            time.sleep(0.1)

        except requests.exceptions.RequestException as e:
            # Log error but continue with what we have
            print(f"\n    [!] Error page {page}: {e} (continuing...)")
            time.sleep(2)
            break

    return properties, total_fetched

def find_max_address(city_id):
    """Find the maximum address number in a city"""
    test_points = [1000, 5000, 10000, 20000, 50000, 100000]
    max_found = 1000
    
    for num in test_points:
        try:
            data = api_get_properties(city_id, page=1, page_size=10, text_search=f"{num}")
            if data.get("totalResults", 0) > 0:
                max_found = num
        except:
            pass
    
    # Refine the max
    for num in range(max_found, max_found + 20000, 1000):
        try:
            data = api_get_properties(city_id, page=1, page_size=10, text_search=f"{num}")
            if data.get("totalResults", 0) > 0:
                max_found = num
            elif num > max_found + 5000:
                break
        except:
            break
    
    return max_found + 1000  # Add buffer

def generate_address_searches(max_address=8000):
    """Generate search patterns that cover all properties globally"""
    searches = []
    
    # Number prefixes with space (covers most US addresses)
    for i in range(1, 10):
        searches.append(f"{i} ")
    for i in range(10, 100):
        searches.append(f"{i} ")
    for i in range(100, 1000):
        searches.append(f"{i} ")
    for i in range(1000, max_address):
        searches.append(f"{i} ")
    
    # Number + letter suffixes (42A, 42B, 42C, etc.)
    for i in range(1, min(1000, max_address)):
        for letter in "ABCDEFGH":
            searches.append(f"{i}{letter} ")
    
    # Higher numbers with letter suffixes
    for i in range(1000, max_address, 10):
        for letter in "ABC":
            searches.append(f"{i}{letter} ")
    
    # Japanese CHOME format (Tokyo, etc.)
    for i in range(1, 10):
        searches.append(f"{i}-CHOME")
    
    # Common international street prefixes
    intl_prefixes = [
        "RUA ", "AVENIDA ", "AV ", "AVENUE ",
        "RUE ", "BOULEVARD ", "PLACE ",
        "STRASSE", "PLATZ ",
        "VIA ", "PIAZZA ",
        "CALLE ", "CARRER ",
    ]
    searches.extend(intl_prefixes)
    
    # Standalone letters (catches edge cases)
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        searches.append(f"{letter} ")
    
    return searches

def build_cache_from_api(city_ids=None, comprehensive=False):
    """Build property cache from Upland API"""
    if not check_api_credentials():
        return

    load_cache()
    initial = len(property_cache)

    mode = "COMPREHENSIVE" if comprehensive else "OPTIMIZED"
    print(f"[*] Mode: {mode} (use --comprehensive for exhaustive search)")
    print("[*] Fetching cities from Upland API...")
    cities = api_get_cities()
    print(f"[+] Found {len(cities)} cities")
    
    for city in cities:
        city_id = city["id"]
        city_name = city["name"]
        
        if city_ids and city_id not in city_ids:
            continue
        
        # Get total property count first
        try:
            data = api_get_properties(city_id, page=1, page_size=100)
            total_props = data.get("totalResults", 0)
            print(f"\n[*] {city_name}: {total_props} total properties")
        except Exception as e:
            print(f"\n[!] Error getting {city_name}: {e}")
            continue
        
        if total_props == 0:
            continue
        
        seen_props = set(property_cache.keys())
        city_total = 0
        
        # Strategy 1: Small city (≤9,900) - simple pagination
        if total_props <= 9900:
            print(f"    [*] Simple pagination (≤9.9k)...")
            city_total = fetch_city_simple(city_id, city_name, total_props, seen_props)
        
        # Strategy 2: Large city - use address number search
        else:
            print(f"    [*] Large city - using address number search...")
            city_total = fetch_city_by_numbers(city_id, city_name, total_props, seen_props, comprehensive=comprehensive)
        
        print(f"\n[+] {city_name}: {city_total} new properties cached")
        save_cache()
    
    new = len(property_cache) - initial
    print(f"\n[+] Added {new} new properties to cache (total: {len(property_cache)})")

def fetch_city_simple(city_id, city_name, total_props, seen_props):
    """Fetch city with simple pagination (for small cities)"""
    city_total = 0
    page = 1
    
    while True:
        try:
            data = api_get_properties(city_id, page=page, page_size=100)
            results = data.get("results", [])
            
            if not results:
                break
            
            for prop in results:
                prop_id = str(prop.get("id"))
                if prop_id not in seen_props:
                    address = prop.get("address", "")
                    neighborhood = prop.get("neighborhood", {}).get("name", "")
                    full_address = f"{address}, {city_name}"
                    if neighborhood:
                        full_address = f"{address}, {neighborhood}, {city_name}"
                    
                    seen_props.add(prop_id)
                    property_cache[prop_id] = full_address
                    city_total += 1
            
            pct = (city_total / total_props * 100) if total_props else 0
            print(f"    Page {page}: {city_total}/{total_props} ({pct:.1f}%)      ", end="\r")
            
            if page >= 99:
                break
            page += 1
            time.sleep(0.1)
            
        except requests.exceptions.RequestException as e:
            print(f"\n    [!] Error page {page}: {e}")
            break
    
    return city_total

def fetch_city_by_numbers(city_id, city_name, total_props, seen_props, comprehensive=False):
    """
    Fetch all properties in a large city by searching address number prefixes.

    Strategy: Search "1 ", "2 ", ... "99999 " to cover all numbered addresses.
    This works because 99.99% of addresses start with a number.
    The 0.4% that start with number+letter (like "479H ") are caught separately.

    Args:
        comprehensive: If True, exhaustive search with no optimizations.
                      If False (default), use smart optimizations for speed.
    """
    city_new = 0  # New properties found this run
    already_cached = 0  # Properties that were already in cache
    REQUEST_DELAY = 0.25  # seconds between requests

    # Optimization settings (disabled in comprehensive mode)
    SKIP_CACHED_THRESHOLD = 0.95  # Skip cities >95% cached (unless comprehensive)
    EARLY_TERM_LIMIT = 300 if not comprehensive else None  # Stop after N empty consecutive searches
    PHASE2_MIN_FOUND = 50 if not comprehensive else 0  # Minimum finds in Phase 1 to run Phase 2
    consecutive_empty = 0  # Track consecutive empty searches for early termination

    def add_results(props):
        nonlocal city_new, already_cached
        for prop_id, address in props:
            if prop_id not in seen_props:
                seen_props.add(prop_id)
                property_cache[prop_id] = address
                city_new += 1
            else:
                already_cached += 1
        return len(props)

    def search_and_add(search_term):
        """Search and add results, returns (count, hit_limit, found_any)"""
        props, count = fetch_properties_with_search(city_id, city_name, text_search=search_term, label=None, quiet=True)
        add_results(props)
        time.sleep(REQUEST_DELAY)
        found_any = count > 0
        return count, count >= 9900, found_any

    # Determine max address number based on city size
    # Higher numbers needed for larger cities with spread-out addresses
    if total_props > 500000:
        max_num = 65000
    elif total_props > 200000:
        max_num = 45000
    elif total_props > 100000:
        max_num = 30000
    elif total_props > 50000:
        max_num = 22000
    elif total_props > 30000:
        max_num = 18000
    elif total_props > 15000:
        max_num = 15000
    else:
        max_num = 12000

    # Count how many properties from this city are already cached
    city_cached = sum(1 for addr in property_cache.values() if city_name in addr)
    pct_cached = (city_cached / total_props * 100) if total_props else 0
    print(f"    [*] Already cached: {city_cached}/{total_props} ({pct_cached:.1f}%)")

    # Skip highly-cached cities in optimized mode
    if not comprehensive and pct_cached >= SKIP_CACHED_THRESHOLD * 100:
        print(f"    [*] City is {pct_cached:.1f}% cached - skipping (use --comprehensive to force search)")
        print(f"    [*] Tip: Run without --build-cache to watch for new listings in real-time")
        return 0

    print(f"    [*] Searching address numbers 1-{max_num} (precise prefix matching)")
    print(f"    [*] Request delay: {REQUEST_DELAY}s")
    if not comprehensive and EARLY_TERM_LIMIT:
        print(f"    [*] Early termination: after {EARLY_TERM_LIMIT} consecutive empty searches")

    # =========================================
    # MAIN SEARCH: Address number prefixes
    # =========================================
    # Search "1 " through "{max_num} " - each is a specific address number
    # This is O(max_num) API calls but very precise

    last_save = 0
    for num in range(1, max_num + 1):
        search = f"{num} "
        count, hit_limit, found_any = search_and_add(search)

        # Track consecutive empty searches for early termination
        if found_any:
            consecutive_empty = 0
        else:
            consecutive_empty += 1

        # Early termination if we've had too many consecutive empty searches
        if EARLY_TERM_LIMIT and consecutive_empty >= EARLY_TERM_LIMIT:
            print(f"\n    [*] Early termination: {consecutive_empty} consecutive empty searches")
            print(f"    [*] Stopped at address number {num}/{max_num}")
            break

        # If we hit the 9900 limit, this number prefix has too many results
        # This shouldn't happen often since "1234 " is very specific
        if hit_limit:
            print(f"\n    [!] '{search}' hit limit ({count}) - searching with street letters...")
            # Subdivide by adding common street starting letters
            for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                sub_search = f"{num} {letter}"
                search_and_add(sub_search)

        # Progress updates - show search progress and new properties found
        if num <= 100:
            if num % 10 == 0:
                print(f"    Searching 1-100: {num}/100 | +{city_new} new                    ", end="\r")
        elif num <= 1000:
            if num % 50 == 0:
                print(f"    Searching 101-1000: {num}/1000 | +{city_new} new               ", end="\r")
        else:
            if num % 500 == 0:
                print(f"    Searching 1001+: {num}/{max_num} | +{city_new} new             ", end="\r")

        # Save periodically (every 500 new properties)
        if city_new - last_save >= 500:
            save_cache()
            last_save = city_new

    print()
    save_cache()

    # =========================================
    # PHASE 2: Number+Letter addresses (like "479H ")
    # =========================================
    # These are ~0.4% of addresses - search common patterns
    phase1_found = city_new

    # Skip Phase 2 if Phase 1 found very few properties (in optimized mode)
    if not comprehensive and phase1_found < PHASE2_MIN_FOUND:
        print(f"\n    [*] Phase 2 skipped: Only {phase1_found} properties found in Phase 1")
        print(f"    [*] Use --comprehensive to search all letter combinations")
    elif city_new > 0 or len(seen_props) < total_props:
        # Reduce scope in optimized mode: 1-300 with A-H instead of 1-999 with A-Z
        max_letter_num = 300 if not comprehensive else 1000
        letters = "ABCDEFGH" if not comprehensive else "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

        print(f"\n    --- Phase 2: Number+Letter patterns (1A-{max_letter_num-1}{letters[-1]}) ---")
        if not comprehensive:
            print(f"    [*] Optimized scope: 1-{max_letter_num-1}, letters {letters}")

        consecutive_empty = 0
        # Search 1A-999Z patterns (covers addresses like "479H DE LONG ST")
        for num in range(1, max_letter_num):
            for letter in letters:
                search = f"{num}{letter} "
                count, _, found_any = search_and_add(search)

                # Track consecutive empty for early termination
                if found_any:
                    consecutive_empty = 0
                else:
                    consecutive_empty += 1

                # Early termination in Phase 2
                if EARLY_TERM_LIMIT and consecutive_empty >= EARLY_TERM_LIMIT:
                    print(f"\n    [*] Phase 2 early termination: {consecutive_empty} consecutive empty")
                    print(f"    [*] Stopped at {num}{letter}")
                    break

            # Break outer loop too if early terminated
            if EARLY_TERM_LIMIT and consecutive_empty >= EARLY_TERM_LIMIT:
                break

            if num % 100 == 0:
                print(f"    1A-{max_letter_num-1}{letters[-1]}: {num}/{max_letter_num-1} | +{city_new} new                ", end="\r")
                save_cache()

        print()
        save_cache()

    # =========================================
    # PHASE 3: International prefixes (if needed)
    # =========================================
    phase2_found = city_new - phase1_found

    # Skip Phase 3 if no results in Phase 2 (in optimized mode)
    if not comprehensive and phase2_found == 0 and phase1_found < PHASE2_MIN_FOUND:
        print(f"\n    [*] Phase 3 skipped: No properties found in Phase 2")
    elif city_new > 0 or len(seen_props) < total_props:
        print(f"\n    --- Phase 3: International/special patterns ---")
        international_prefixes = [
            "RUA ", "AVENIDA ", "CALLE ", "VIA ", "RUE ", "STRASSE",
            "1-CHOME", "2-CHOME", "3-CHOME", "4-CHOME", "5-CHOME",
            "6-CHOME", "7-CHOME", "8-CHOME", "9-CHOME",
            "TERMINAL", "PIER", "STATION", "AIRPORT",
        ]
        for prefix in international_prefixes:
            count, _, _ = search_and_add(prefix)
            if count > 0:
                print(f"    '{prefix}': +{city_new} new total")
        save_cache()

    # =========================================
    # Final summary
    # =========================================
    phase3_found = city_new - phase1_found - phase2_found
    final_cached = sum(1 for addr in property_cache.values() if city_name in addr)
    final_pct = (final_cached / total_props * 100) if total_props else 0

    print(f"\n    ╔═══ SUMMARY ═══")
    print(f"    ║ Phase 1 (numbers):  +{phase1_found}")
    print(f"    ║ Phase 2 (letters):  +{phase2_found}")
    print(f"    ║ Phase 3 (intl):     +{phase3_found}")
    print(f"    ║ ─────────────────")
    print(f"    ║ Total new:          +{city_new}")
    print(f"    ║ Total cached:       {final_cached}/{total_props} ({final_pct:.1f}%)")
    print(f"    ║ Global cache:       {len(property_cache)} properties")
    print(f"    ╚═══════════════")

    save_cache()
    return city_new

# ============================================================
# Blockchain Functions
# ============================================================

def get_actions(account=None, limit=50, after=None):
    params = {"limit": limit, "sort": "desc"}
    if account:
        params["account"] = account
    if after:
        params["after"] = after
    
    r = requests.get(f"{CHAIN_URL}/v2/history/get_actions", params=params, headers=HEADERS)
    r.raise_for_status()
    return r.json()

def decode_listing(action):
    """Decode a listing-related action"""
    act = action.get("act", {})
    name = act.get("name", "")
    data = act.get("data", {})
    
    decoded = {PARAM_MAP.get(k, k): v for k, v in data.items()}
    
    event_type = LISTING_ACTIONS.get(name, name.upper())
    property_id = decoded.get("property_id")
    
    result = {
        "event": event_type,
        "time": action.get("@timestamp", "")[:19].replace("T", " "),
        "block": action.get("block_num"),
        "trx_id": action.get("trx_id", ""),
        "property_id": property_id,
        "address": get_address(property_id),
    }
    
    if name == "n2":
        result["seller"] = decoded.get("seller")
        result["upx_price"] = decoded.get("upx_price")
        result["fiat_price"] = decoded.get("fiat_price")
    elif name == "n4":
        result["owner"] = decoded.get("seller")
    elif name == "n5":
        result["buyer"] = decoded.get("buyer")
        result["sale_price"] = decoded.get("sale_price")
        memo = decoded.get("memo", "")
        address = extract_address_from_memo(memo)
        if address and property_id:
            cache_property(property_id, address)
            result["address"] = address
            save_cache()
    
    return result

def print_listing(listing):
    """Pretty print a listing event"""
    event = listing["event"]
    
    colors = {
        "LISTED_FOR_SALE": ("\033[92m", "🏷️ "),
        "UNLISTED": ("\033[93m", "❌ "),
        "SALE_COMPLETE": ("\033[96m", "💰 "),
    }
    color, icon = colors.get(event, ("\033[0m", "📋 "))
    reset = "\033[0m"
    
    print(f"{color}┌─ {icon}{event} ─────────────────────────{reset}")
    print(f"│ Time:       {listing['time']}")
    print(f"│ Property:   {listing['property_id']}")
    
    if listing.get('address'):
        print(f"│ Address:    {listing['address']}")
    
    if event == "LISTED_FOR_SALE":
        print(f"│ Seller:     {listing.get('seller')}")
        print(f"│ UPX Price:  {listing.get('upx_price')}")
        if listing.get('fiat_price') and listing['fiat_price'] != "0.00 FIAT":
            print(f"│ USD Price:  {listing.get('fiat_price')}")
    elif event == "UNLISTED":
        print(f"│ Owner:      {listing.get('owner')}")
    elif event == "SALE_COMPLETE":
        print(f"│ Buyer:      {listing.get('buyer')}")
        print(f"│ Price:      {listing.get('sale_price')}")
    
    print(f"└─ trx: {listing['trx_id'][:40]}...")
    print()

def watch_listings(interval=5, events=None):
    """Watch for property listings in real-time"""
    load_cache()
    
    if events is None:
        events = ["n2", "n4", "n5"]
    
    event_names = [LISTING_ACTIONS.get(e, e) for e in events]
    
    print(f"╔═══════════════════════════════════════════════════════")
    print(f"║ 🏠 Upland Property Listings Tracker")
    print(f"║ Watching: {', '.join(event_names)}")
    print(f"║ Cache: {len(property_cache)} properties")
    print(f"║ Interval: {interval}s")
    print(f"╚═══════════════════════════════════════════════════════\n")
    
    seen_txs = set()
    last_timestamp = datetime.now(timezone.utc).isoformat()
    
    while True:
        try:
            data = get_actions(account="playuplandme", limit=100, after=last_timestamp)
            actions = data.get("actions", [])
            
            for action in reversed(actions):
                trx_id = action.get("trx_id")
                global_seq = action.get("global_sequence")
                uid = f"{trx_id}:{global_seq}"
                act_name = action.get("act", {}).get("name", "")
                
                if act_name not in events:
                    continue
                
                if uid not in seen_txs:
                    seen_txs.add(uid)
                    listing = decode_listing(action)
                    print_listing(listing)
                    
                    ts = action.get("@timestamp")
                    if ts and ts > last_timestamp:
                        last_timestamp = ts
            
            if len(seen_txs) > 10000:
                seen_txs = set(list(seen_txs)[-5000:])
                
        except requests.exceptions.RequestException as e:
            print(f"[!] Request error: {e}")
        except Exception as e:
            print(f"[!] Error: {e}")
        
        time.sleep(interval)

def dump_listings(limit=200, events=None):
    """One-shot dump of recent listing events"""
    load_cache()
    
    if events is None:
        events = ["n2", "n4", "n5"]
    
    print(f"[*] Fetching last {limit} actions...\n")
    
    data = get_actions(account="playuplandme", limit=limit)
    actions = data.get("actions", [])
    
    found = 0
    for action in actions:
        act_name = action.get("act", {}).get("name", "")
        if act_name in events:
            listing = decode_listing(action)
            print_listing(listing)
            found += 1
    
    print(f"[+] Found {found} listing events")

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Track Upland property listings")
    parser.add_argument("--build-cache", action="store_true", help="Build cache from Upland API")
    parser.add_argument("--city", type=int, action="append", help="City ID to cache (can use multiple)")
    parser.add_argument("--comprehensive", action="store_true", help="Comprehensive search (no optimizations, slower but thorough)")
    parser.add_argument("--dump", action="store_true", help="Dump recent listings")
    parser.add_argument("-n", "--limit", type=int, default=500, help="Actions to scan")
    parser.add_argument("-i", "--interval", type=int, default=5, help="Poll interval")
    parser.add_argument("--sales-only", action="store_true", help="Only show sales")
    parser.add_argument("--new-only", action="store_true", help="Only show new listings")
    
    args = parser.parse_args()
    
    if args.sales_only:
        events = ["n5"]
    elif args.new_only:
        events = ["n2"]
    else:
        events = ["n2", "n4", "n5"]
    
    if args.build_cache:
        build_cache_from_api(city_ids=args.city, comprehensive=args.comprehensive)
    elif args.dump:
        dump_listings(limit=args.limit, events=events)
    else:
        watch_listings(interval=args.interval, events=events)