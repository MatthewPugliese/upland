"""
UplandScope — Precache a user's own neighborhoods with real live status

Unlike precache_neighborhoods.py (which writes placeholder status="Unknown"
for every property from the offline property_cache.json — and is therefore
never actually used by forsale_finder._load_neighborhood_cache, which
explicitly rejects an all-"Unknown" cache and falls back to a live scan),
this fetches REAL status from the live Upland API for a specific user's own
neighborhoods, writing cache files in the exact format the fast path expects.

Without this, every Floor Price Tracker / Collection Map / Find Listings
lookup for a neighborhood with no existing fast-path cache re-runs the full
slow-path city scan from scratch, every single time — there's no caching at
the candidate-list level today. This runs once and permanently speeds up
(and keeps correct) lookups for the given account's own neighborhoods.

Usage: python3 precache_my_neighborhoods.py <username> <eos_account>
"""

import json
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / "cache" / "neighborhoods"

from collection_optimizer import load_user_properties
from forsale_finder import _get_neighborhood_candidates
from neighborhoods import get_all_neighborhoods


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in " -_" else "_" for c in name).strip().replace(" ", "_")


def main(username: str, eos_account: str) -> None:
    props = load_user_properties(username, eos_account)
    if not props:
        print(f"No properties found for {username}.")
        return

    hoods = sorted({(p["neighborhood"], p["city"]) for p in props if p.get("neighborhood")})
    print(f"[+] {len(props)} properties across {len(hoods)} unique neighborhoods")

    all_hoods = get_all_neighborhoods()
    city_id_by_key = {(h["name"].upper(), h["city_name"]): h["city_id"] for h in all_hoods}

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached, skipped, empty, errors = 0, 0, 0, 0

    for i, (hood, city) in enumerate(hoods, 1):
        cache_path = CACHE_DIR / f"{_safe_name(hood)}_props_cache.json"
        if cache_path.exists():
            print(f"[{i}/{len(hoods)}] {hood} ({city}) — already cached, skip")
            skipped += 1
            continue

        city_id = city_id_by_key.get((hood.upper(), city))
        if not city_id:
            print(f"[{i}/{len(hoods)}] {hood} ({city}) — no city_id resolved, skip")
            empty += 1
            continue

        try:
            candidates = _get_neighborhood_candidates(
                {"type": "neighborhood", "neighborhood": hood}, city_id
            )
            if candidates:
                cache_path.write_text(json.dumps(candidates))
                print(f"[{i}/{len(hoods)}] {hood} ({city}) — cached {len(candidates)} properties")
                cached += 1
            else:
                print(f"[{i}/{len(hoods)}] {hood} ({city}) — 0 candidates found")
                empty += 1
        except Exception as e:
            print(f"[{i}/{len(hoods)}] {hood} ({city}) — error: {e}")
            errors += 1

        time.sleep(1)  # gentle pacing between neighborhoods, on top of internal API pacing

    print(f"\n{'='*50}")
    print(f"  Cached:  {cached}")
    print(f"  Skipped (already cached): {skipped}")
    print(f"  Empty/no candidates: {empty}")
    print(f"  Errors:  {errors}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python3 precache_my_neighborhoods.py <username> <eos_account>")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
