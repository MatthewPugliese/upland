"""
UplandScope — Floor Price Tracker

Given a neighborhood, finds every currently "For sale" property there and
reports the floor (lowest) UPX and USD asking price, plus the full sorted
listing table for each currency.

Uses stored listings when the snapshot has any for this neighborhood.
Otherwise it prices for-sale rows in the local neighborhood file, up to 60,
from the public property page. It does not scan a city. A property that
sold after the file was saved usually has no live price and is left out.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import sys

from forsale_finder import _load_neighborhood_cache, _public_api_price  # noqa: E402
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


def floor_from_snapshot(hood_name: str, city_name: str) -> dict | None:
    """Lowest stored asking prices for this neighborhood, or None if none are stored.

    None leaves the live lookup in place. A returned floor is only the local
    snapshot, not every listing in the neighborhood.
    """
    scraper_dir = str(SCRIPT_DIR.parent / "scraper")
    if scraper_dir not in sys.path:
        sys.path.insert(0, scraper_dir)
    try:
        from listings_db import DEFAULT_DB, connect, partial_snapshot_note, search_listings
    except Exception:
        return None
    if not DEFAULT_DB.exists():
        return None

    conn = connect(DEFAULT_DB)
    try:
        rows = search_listings(conn, neighborhood=hood_name, limit=500)
        if not rows:
            return None
        note = partial_snapshot_note(conn)
    except Exception:
        return None
    finally:
        conn.close()

    listings = []
    for row in rows:
        currency = row.get("currency") or ""
        price = row.get("price_upx") if currency == "UPX" else row.get("price_usd")
        if price is None:
            continue
        listings.append({
            "prop_id": str(row.get("id")),
            "address": row.get("address") or "",
            "currency": currency,
            "price": price,
            "mint_price": row.get("mint_price"),
            "markup_pct": row.get("markup_pct"),
            "owner_username": row.get("owner_username") or "",
        })
    if not listings:
        return None

    upx_listings = sorted((item for item in listings if item["currency"] == "UPX"), key=lambda item: item["price"])
    usd_listings = sorted((item for item in listings if item["currency"] == "USD"), key=lambda item: item["price"])
    return {
        "neighborhood": hood_name,
        "city": city_name,
        "total_for_sale": len(listings),
        "priced_count": len(listings),
        "truncated": len(rows) >= 500,
        "coverage": "partial",
        "note": note,
        "floor_upx": upx_listings[0]["price"] if upx_listings else None,
        "floor_upx_address": upx_listings[0]["address"] if upx_listings else None,
        "floor_usd": usd_listings[0]["price"] if usd_listings else None,
        "floor_usd_address": usd_listings[0]["address"] if usd_listings else None,
        "upx_listings": upx_listings,
        "usd_listings": usd_listings,
    }


def _priced_listings(for_sale: list) -> list:
    """Ask the public property page for a price. Capped. Does not scan a city."""
    listings = []
    for prop in for_sale[:MAX_LISTINGS_PRICED]:
        prop_id = prop.get("id") or prop.get("prop_id")
        price_upx, price_usd, currency, owner = _public_api_price(prop_id)
        if price_upx is None and price_usd is None:
            continue
        mint = prop.get("mintPrice")
        price = price_upx if currency == "UPX" else price_usd
        markup_pct = (
            round(100 * (price - mint) / mint, 1)
            if (currency == "UPX" and mint and price) else None
        )
        listings.append({
            "prop_id": str(prop_id),
            "address": prop.get("address") or prop.get("full_address", ""),
            "currency": currency,
            "price": price,
            "mint_price": mint,
            "markup_pct": markup_pct,
            "owner_username": owner or "",
        })
    return listings


def _floor_result(hood_name: str, city_name: str, listings: list, **extra) -> dict:
    upx_listings = sorted((item for item in listings if item["currency"] == "UPX"), key=lambda item: item["price"])
    usd_listings = sorted((item for item in listings if item["currency"] == "USD"), key=lambda item: item["price"])
    result = {
        "neighborhood": hood_name,
        "city": city_name,
        "total_for_sale": extra.get("total_for_sale", len(listings)),
        "priced_count": len(listings),
        "truncated": extra.get("truncated", False),
        "coverage": extra.get("coverage"),
        "note": extra.get("note"),
        "floor_upx": upx_listings[0]["price"] if upx_listings else None,
        "floor_upx_address": upx_listings[0]["address"] if upx_listings else None,
        "floor_usd": usd_listings[0]["price"] if usd_listings else None,
        "floor_usd_address": usd_listings[0]["address"] if usd_listings else None,
        "upx_listings": upx_listings,
        "usd_listings": usd_listings,
    }
    return result


def _floor_from_neighborhood_file(hood_name: str, city_name: str) -> dict | None:
    """For-sale rows in the local neighborhood file, with live prices. No city scan."""
    props = _load_neighborhood_cache(hood_name, allow_unknown=True)
    if not props:
        return None
    for_sale = [prop for prop in props if prop.get("status") == "For sale"]
    if not for_sale:
        return _floor_result(
            hood_name, city_name, [],
            total_for_sale=0,
            coverage="neighborhood",
            note="The local neighborhood file has no for-sale rows. A city scan was not started.",
        )
    listings = _priced_listings(for_sale)
    return _floor_result(
        hood_name, city_name, listings,
        total_for_sale=len(for_sale),
        truncated=len(for_sale) > MAX_LISTINGS_PRICED,
        coverage="neighborhood",
        note="Prices are for rows marked for sale in the local neighborhood file. A city scan was not started.",
    )


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
            return _attach_sale_comparison(json.loads(cache_path.read_text()))

    # Stored prices answer the neighborhood without calling Upland. No rows
    # means this snapshot cannot name a floor. The neighborhood file is the
    # next local source. A city scan is not started.
    snapshot = floor_from_snapshot(hood_name, hood["city_name"])
    if snapshot is not None:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(snapshot))
        return _attach_sale_comparison(snapshot)

    local = _floor_from_neighborhood_file(hood_name, hood["city_name"])
    if local is not None:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(local))
        return _attach_sale_comparison(local)

    return {"error": f"No stored listings for {hood_name}. A city scan was not started."}


def _attach_sale_comparison(result: dict) -> dict:
    """Add the recent UPX sale median. Does not change stored asking prices."""
    if not result or result.get("error"):
        return result
    try:
        from economy import neighborhood_sale_medians
        medians = neighborhood_sale_medians([result.get("neighborhood") or ""])
    except Exception:
        medians = {}
    folded = {name.casefold(): info for name, info in medians.items()}
    info = folded.get((result.get("neighborhood") or "").casefold())
    median = info.get("median_upx") if info else None
    result["sale_median_upx"] = median
    result["sale_count"] = info.get("sales") if info else None
    result["sale_window_days"] = 90
    compared = 0
    at_or_under = 0
    for listing in result.get("upx_listings") or []:
        price = listing.get("price")
        if median and price:
            listing["vs_median_pct"] = round(100 * (price - median) / median, 1)
            compared += 1
            if listing["vs_median_pct"] <= 0:
                at_or_under += 1
        else:
            listing["vs_median_pct"] = None
    result["asks_compared"] = compared
    result["asks_at_or_under"] = at_or_under
    return result
