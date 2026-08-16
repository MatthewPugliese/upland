"""
UplandScope — Floor Price Tracker

Given a neighborhood, finds every currently "For sale" property there and
reports the floor (lowest) UPX and USD asking price, plus the full sorted
listing table for each currency.

Reuses forsale_finder's neighborhood candidate lookup (fast path: webapp's
precached {hood}_props_cache.json — ~1,900 neighborhoods already have one;
slow path: live Developers API scan) and the same per-property live price
fetch (`_public_api_price`) used there and by the Collection Tracker's
for-sale finder. Property statuses in the cache can be stale (last
precached, not live) — self-corrects naturally, since a property that's
sold since the cache was built simply returns no live listing price and is
dropped from the results.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from forsale_finder import _get_neighborhood_candidates, _public_api_price  # noqa: E402
from neighborhoods import get_all_neighborhoods

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / "cache" / "floor_price"
FLOOR_CACHE_TTL = 1800  # 30 min — matches forsale_finder's live-price cache convention
MAX_LISTINGS_PRICED = 60  # cap live price fetches per neighborhood


def _resolve_neighborhood(name: str) -> dict | None:
    name_upper = name.strip().upper()
    all_hoods = get_all_neighborhoods()
    exact = [h for h in all_hoods if h["name"].upper() == name_upper]
    if exact:
        return exact[0]
    partial = [h for h in all_hoods if name_upper in h["name"].upper()]
    return partial[0] if partial else None


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name.upper())


def get_floor_prices(neighborhood: str) -> dict:
    """
    Returns {"error": "..."} if the neighborhood can't be resolved, otherwise:
    {neighborhood, city, total_for_sale, priced_count, truncated,
     floor_upx, floor_upx_address, floor_usd, floor_usd_address,
     upx_listings: [...], usd_listings: [...]}
    each listing: {prop_id, address, price, mint_price, markup_pct, owner_username}
    """
    hood = _resolve_neighborhood(neighborhood)
    if not hood:
        return {"error": f"Unknown neighborhood '{neighborhood}'."}

    hood_name = hood["name"]
    cache_path = CACHE_DIR / f"{_safe_name(hood_name)}.json"
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < FLOOR_CACHE_TTL:
            return json.loads(cache_path.read_text())

    parsed_req = {"type": "neighborhood", "neighborhood": hood_name}
    candidates = _get_neighborhood_candidates(parsed_req, hood["city_id"])
    for_sale = [c for c in candidates if c.get("status") == "For sale"]

    listings = []
    for c in for_sale[:MAX_LISTINGS_PRICED]:
        pid = c.get("id") or c.get("prop_id")
        price_upx, price_usd, currency, owner = _public_api_price(pid)
        if price_upx is None and price_usd is None:
            continue  # no longer actually listed (stale cache) or fetch failed
        mint = c.get("mintPrice")
        price = price_upx if currency == "UPX" else price_usd
        # markup % only makes sense against mint price in the same currency (UPX) —
        # mint price is always in UPX, so a USD listing has no comparable mint figure
        # without a live exchange rate, which isn't available here.
        markup_pct = (round(100 * (price - mint) / mint, 1)
                      if (currency == "UPX" and mint and price) else None)
        listings.append({
            "prop_id": str(pid),
            "address": c.get("address") or c.get("full_address", ""),
            "currency": currency,
            "price": price,
            "mint_price": mint,
            "markup_pct": markup_pct,
            "owner_username": owner,
        })

    upx_listings = sorted([l for l in listings if l["currency"] == "UPX"], key=lambda x: x["price"])
    usd_listings = sorted([l for l in listings if l["currency"] == "USD"], key=lambda x: x["price"])

    result = {
        "neighborhood": hood_name,
        "city": hood["city_name"],
        "total_for_sale": len(for_sale),
        "priced_count": len(listings),
        "truncated": len(for_sale) > MAX_LISTINGS_PRICED,
        "floor_upx": upx_listings[0]["price"] if upx_listings else None,
        "floor_upx_address": upx_listings[0]["address"] if upx_listings else None,
        "floor_usd": usd_listings[0]["price"] if usd_listings else None,
        "floor_usd_address": usd_listings[0]["address"] if usd_listings else None,
        "upx_listings": upx_listings,
        "usd_listings": usd_listings,
    }

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(result))
    return result
