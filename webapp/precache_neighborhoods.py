#!/usr/bin/env python3
"""
Pre-cache neighborhood property lists from the main property_cache.json.

This avoids slow API enrichment by extracting properties directly from the
4.7M property cache built by listings.py. Properties will have addresses and
IDs but status will be "Unknown" until the Upland API is queried.

Usage:
    python3 precache_neighborhoods.py              # Cache all neighborhoods
    python3 precache_neighborhoods.py --city "Manhattan"  # Just one city
    python3 precache_neighborhoods.py --dry-run     # Show what would be cached

Reads:
    ../upland-monitor/property_cache.json  (4.7M properties)
    cache/neighborhoods_all.json           (neighborhood list)

Writes:
    cache/neighborhoods/{Name}_props_cache.json  (per-neighborhood)
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / "cache" / "neighborhoods"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

MAIN_CACHE_CANDIDATES = [
    SCRIPT_DIR.parent / "upland-monitor" / "property_cache.json",
    SCRIPT_DIR.parent / "property_cache.json",
]

HOOD_LIST = SCRIPT_DIR / "cache" / "neighborhoods_all.json"


def load_main_cache() -> dict:
    for path in MAIN_CACHE_CANDIDATES:
        if path.exists():
            print(f"[*] Loading main property cache from {path}...")
            t0 = time.time()
            with open(path) as f:
                data = json.load(f)
            print(f"[+] {len(data):,} properties loaded in {time.time()-t0:.1f}s")
            return data
    print("[!] No property cache found")
    sys.exit(1)


def load_hood_list() -> list:
    if not HOOD_LIST.exists():
        print(f"[!] No neighborhood list at {HOOD_LIST}")
        print("    Run the webapp first to cache it, or run:")
        print("    cd webapp && python3 -c 'from neighborhoods import get_all_neighborhoods; get_all_neighborhoods()'")
        sys.exit(1)
    with open(HOOD_LIST) as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(description="Pre-cache neighborhood property lists")
    parser.add_argument("--city", help="Only cache neighborhoods in this city")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be cached")
    parser.add_argument("--force", action="store_true", help="Overwrite existing caches")
    args = parser.parse_args()

    hoods = load_hood_list()
    if args.city:
        hoods = [h for h in hoods if args.city.lower() in h["city_name"].lower()]
    print(f"[*] {len(hoods)} neighborhoods to process")

    if args.dry_run:
        for h in hoods:
            safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in h["name"]).strip().replace(" ", "_")
            cache_path = CACHE_DIR / f"{safe}_props_cache.json"
            exists = "EXISTS" if cache_path.exists() else "MISSING"
            print(f"  [{exists}] {h['name']} ({h['city_name']})")
        already = sum(1 for h in hoods
                      if (CACHE_DIR / f'{"".join(c if c.isalnum() or c in " -_" else "_" for c in h["name"]).strip().replace(" ", "_")}_props_cache.json').exists())
        print(f"\n{already} already cached, {len(hoods) - already} remaining")
        return

    # Load the big cache
    main_cache = load_main_cache()

    # Index by neighborhood name (2nd comma-delimited segment)
    print("[*] Indexing properties by neighborhood...")
    by_hood = defaultdict(list)
    for prop_id, full_addr in main_cache.items():
        parts = [p.strip() for p in full_addr.split(",")]
        if len(parts) >= 2:
            hood_name = parts[1].upper()
            by_hood[hood_name].append({
                "id": prop_id,
                "address": parts[0],
                "status": "Unknown",
                "mintPrice": None,
                "neighborhood": {"name": hood_name},
                "city": {"name": parts[2] if len(parts) >= 3 else ""},
            })

    print(f"[+] Indexed {len(by_hood)} unique neighborhood names")

    # Write caches
    cached = 0
    skipped = 0
    empty = 0

    for h in hoods:
        safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in h["name"]).strip().replace(" ", "_")
        cache_path = CACHE_DIR / f"{safe}_props_cache.json"

        if cache_path.exists() and not args.force:
            skipped += 1
            continue

        hood_key = h["name"].upper()
        props = by_hood.get(hood_key, [])

        if not props:
            empty += 1
            continue

        with open(cache_path, "w") as f:
            json.dump(props, f)
        cached += 1

        if cached % 50 == 0:
            print(f"  [{cached}/{len(hoods)}] {h['name']} ({h['city_name']}): {len(props)} properties")

    print(f"\n{'='*50}")
    print(f"  Cached:  {cached} neighborhoods")
    print(f"  Skipped: {skipped} (already exist)")
    print(f"  Empty:   {empty} (not in property cache)")
    print(f"  Total:   {cached + skipped + empty}/{len(hoods)}")
    print(f"  Output:  {CACHE_DIR}")


if __name__ == "__main__":
    main()
