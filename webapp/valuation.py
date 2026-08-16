"""
UplandScope — Property Valuation Tool

Given a property (in-game address text, or a numeric property ID), estimates
fair market value from comparable recent sales in the same neighborhood,
broadening to city-level (and to a wider time window) when neighborhood comps
are sparse.

Address/neighborhood/city resolution uses scraper/property_cache.db (the
scraper's prop_id -> address/neighborhood/city cache, ~4.7M properties).
Live property details (UP², current listing price/currency, placed
structures) come from the public Upland API — same endpoint used by
portfolio_analyzer/forsale_finder. Comparable sales come from
data/economy.db's transactions table (n5 = UPX sale, n52 = USD sale), joined
against property_cache.db (via ATTACH DATABASE) to get each comp's
neighborhood/city, since most transactions rows don't have those columns
populated directly (only ~13% do — see docs/PROPERTY_VALUATION_PLAN.md).
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import sqlite3
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROP_CACHE_DB = SCRIPT_DIR.parent / "scraper" / "property_cache.db"
ECONOMY_DB = Path(os.environ.get("ECONOMY_DB", str(SCRIPT_DIR.parent / "data" / "economy.db")))
AREA_CACHE_PATH = SCRIPT_DIR / "cache" / "valuation" / "area_cache.json"
AREA_CACHE_TTL = 30 * 86400  # UP² never changes — long TTL

COMP_WINDOWS_DAYS = [90, 180, 365]
MIN_COMPS_NEIGHBORHOOD = 5
MIN_COMPS_CITY = 5
MAX_COMPS_SHOWN = 20
MAX_AREA_LOOKUPS = 60


def _pc_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(PROP_CACHE_DB), timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def resolve_property(query: str) -> dict:
    """
    Resolve an address string or numeric property ID to candidate rows from
    property_cache.db. Returns {"matches": [{prop_id, address, neighborhood,
    city}, ...]} — empty, one, or many (caller should disambiguate if >1).
    """
    query = query.strip()
    if not query:
        return {"matches": []}
    conn = _pc_connect()
    try:
        if query.isdigit():
            rows = conn.execute(
                "SELECT prop_id, address, neighborhood, city FROM properties WHERE prop_id = ?",
                (query,),
            ).fetchall()
        else:
            like = f"%{query.upper()}%"
            rows = conn.execute(
                "SELECT prop_id, address, neighborhood, city FROM properties "
                "WHERE address LIKE ? LIMIT 25",
                (like,),
            ).fetchall()
        return {"matches": [dict(r) for r in rows]}
    finally:
        conn.close()


def fetch_property_live(prop_id: str) -> dict | None:
    """Fetch full live property data from the public Upland API."""
    try:
        req = urllib.request.Request(
            f"https://api.upland.me/properties/{prop_id}",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _load_area_cache() -> dict:
    if AREA_CACHE_PATH.exists():
        try:
            return json.loads(AREA_CACHE_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save_area_cache(cache: dict) -> None:
    AREA_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    AREA_CACHE_PATH.write_text(json.dumps(cache))


def fetch_areas(prop_ids: list) -> dict:
    """Return {prop_id: area|None} for a list of property IDs, using a
    long-lived disk cache (UP² is static) to avoid re-fetching comps that
    keep coming up across different valuation queries in the same neighborhood."""
    cache = _load_area_cache()
    now = time.time()
    result = {}
    need = []
    for pid in prop_ids:
        entry = cache.get(pid)
        if entry and now - entry.get("ts", 0) < AREA_CACHE_TTL:
            result[pid] = entry.get("area")
        else:
            need.append(pid)

    if need:
        def _fetch_one(pid):
            try:
                req = urllib.request.Request(
                    f"https://api.upland.me/properties/{pid}",
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read())
                return pid, data.get("area")
            except Exception:
                return pid, None

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(_fetch_one, pid) for pid in need]
            for future in concurrent.futures.as_completed(futures):
                pid, area = future.result()
                result[pid] = area
                cache[pid] = {"area": area, "ts": now}
        _save_area_cache(cache)

    return result


def _days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


def _query_comps(conn: sqlite3.Connection, where_clause: str, params: tuple, since: str) -> list:
    rows = conn.execute(f"""
        SELECT t.property_id, t.timestamp, t.action, t.upx_amount, t.usd_amount,
               p.address, p.city, p.neighborhood
        FROM transactions t
        JOIN pc.properties p ON t.property_id = p.prop_id
        WHERE t.asset_type = 'property'
          AND t.action IN ('n5', 'n52')
          AND t.timestamp >= ?
          AND {where_clause}
        ORDER BY t.timestamp DESC
        LIMIT 300
    """, (since, *params)).fetchall()
    return [dict(r) for r in rows]


def find_comps(neighborhood: str, city: str, exclude_prop_id: str | None = None) -> dict:
    """
    Find comparable sales, preferring same-neighborhood and a tight time
    window, broadening the window first and then to city-level if the
    neighborhood doesn't have enough recent sales.
    Returns {"comps": [...], "scope": "neighborhood"|"city", "window_days": N}.
    """
    conn = sqlite3.connect(str(ECONOMY_DB), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute(f"ATTACH DATABASE '{PROP_CACHE_DB}' AS pc")
    try:
        neighborhood_comps, n_days = [], COMP_WINDOWS_DAYS[-1]
        for days in COMP_WINDOWS_DAYS:
            rows = _query_comps(conn, "p.neighborhood = ?", (neighborhood,), _days_ago(days))
            if exclude_prop_id:
                rows = [r for r in rows if r["property_id"] != exclude_prop_id]
            neighborhood_comps, n_days = rows, days
            if len(rows) >= 10:
                break

        if len(neighborhood_comps) >= MIN_COMPS_NEIGHBORHOOD:
            return {"comps": neighborhood_comps, "scope": "neighborhood", "window_days": n_days}

        city_comps, c_days = [], COMP_WINDOWS_DAYS[-1]
        for days in COMP_WINDOWS_DAYS:
            rows = _query_comps(conn, "p.city = ?", (city,), _days_ago(days))
            if exclude_prop_id:
                rows = [r for r in rows if r["property_id"] != exclude_prop_id]
            city_comps, c_days = rows, days
            if len(rows) >= MIN_COMPS_CITY:
                break

        if city_comps:
            return {"comps": city_comps, "scope": "city", "window_days": c_days}
        return {"comps": neighborhood_comps, "scope": "neighborhood", "window_days": n_days}
    finally:
        conn.close()


def _median(values: list) -> float | None:
    if not values:
        return None
    values = sorted(values)
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2


def _confidence_tier(count: int, scope: str) -> tuple:
    """Returns (label, css_slug)."""
    if scope == "neighborhood":
        if count >= 10:
            return "High", "high"
        if count >= MIN_COMPS_NEIGHBORHOOD:
            return "Medium", "medium"
        return "Very low — insufficient data", "very-low"
    if count >= MIN_COMPS_CITY:
        return "Low — broadened", "low"
    return "Very low — insufficient data", "very-low"


def _build_currency_valuation(comps: list, scope: str, window_days: int, amount_key: str,
                               target_area: float, areas: dict) -> dict:
    priced = []
    for c in comps:
        amount = c.get(amount_key)
        area = areas.get(c["property_id"])
        if amount is None or not area:
            continue
        priced.append({**c, "area": area, "per_up2": round(amount / area, 2)})
    priced.sort(key=lambda c: c["timestamp"], reverse=True)

    count = len(priced)
    median_per_up2 = _median([p["per_up2"] for p in priced])
    estimated_value = round(median_per_up2 * target_area) if median_per_up2 and target_area else None
    confidence, confidence_slug = _confidence_tier(count, scope)

    return {
        "comps": priced[:MAX_COMPS_SHOWN],
        "comp_count": count,
        "scope": scope,
        "window_days": window_days,
        "median_per_up2": median_per_up2,
        "estimated_value": estimated_value,
        "confidence": confidence,
        "confidence_slug": confidence_slug,
    }


def neighborhood_valuation_rate(neighborhood: str, city: str) -> dict:
    """
    Median UPX/UP² and USD/UP² for an entire neighborhood, not tied to one
    target property. Used by the Portfolio Analyzer to estimate a portfolio's
    current market value without running a full comp search per property —
    one comp search per *neighborhood* is applied against however many UP²
    the portfolio holds there.
    """
    comp_result = find_comps(neighborhood, city)
    comps = comp_result["comps"]
    prop_ids = list({c["property_id"] for c in comps})[:MAX_AREA_LOOKUPS]
    areas = fetch_areas(prop_ids)

    upx_val = _build_currency_valuation(comps, comp_result["scope"], comp_result["window_days"],
                                         "upx_amount", 0, areas)
    usd_val = _build_currency_valuation(comps, comp_result["scope"], comp_result["window_days"],
                                         "usd_amount", 0, areas)
    return {
        "upx_per_up2": upx_val["median_per_up2"],
        "upx_comp_count": upx_val["comp_count"],
        "upx_confidence": upx_val["confidence"],
        "usd_per_up2": usd_val["median_per_up2"],
        "usd_comp_count": usd_val["comp_count"],
        "usd_confidence": usd_val["confidence"],
    }


def estimate_value(query: str) -> dict:
    """
    Main entry point. Returns one of:
      - {"error": "..."} — nothing matched, or live API fetch failed
      - {"matches": [...]} — more than one property matched the query text,
        caller should show a disambiguation list
      - full result dict with "property", "upx_valuation", "usd_valuation"
    """
    resolved = resolve_property(query)
    matches = resolved["matches"]
    if not matches:
        return {"error": f"No property found matching '{query}'.", "matches": []}
    if len(matches) > 1:
        return {"matches": matches}

    match = matches[0]
    prop_id = match["prop_id"]
    live = fetch_property_live(prop_id)
    if not live:
        return {"error": f"Couldn't fetch live data for property {prop_id} from the Upland API.",
                 "matches": []}

    target_area = live.get("area") or 0
    on_market = live.get("on_market") or {}
    is_listed = bool(live.get("on_market"))
    listing_currency = on_market.get("currency", "") if is_listed else None
    listing_price = live.get("price") if is_listed else None
    buildings = live.get("buildings") or []

    comp_result = find_comps(match["neighborhood"], match["city"], exclude_prop_id=prop_id)
    comps = comp_result["comps"]

    prop_ids_needed = list({c["property_id"] for c in comps})[:MAX_AREA_LOOKUPS]
    areas = fetch_areas(prop_ids_needed)

    upx_val = _build_currency_valuation(comps, comp_result["scope"], comp_result["window_days"],
                                         "upx_amount", target_area, areas)
    usd_val = _build_currency_valuation(comps, comp_result["scope"], comp_result["window_days"],
                                         "usd_amount", target_area, areas)

    if is_listed and listing_price is not None:
        if listing_currency == "UPX" and upx_val["estimated_value"]:
            upx_val["listing_pct_diff"] = round(
                100 * (listing_price - upx_val["estimated_value"]) / upx_val["estimated_value"], 1)
        if listing_currency == "USD" and usd_val["estimated_value"]:
            usd_val["listing_pct_diff"] = round(
                100 * (listing_price - usd_val["estimated_value"]) / usd_val["estimated_value"], 1)

    return {
        "matches": [],
        "property": {
            "prop_id": prop_id,
            "address": match["address"],
            "neighborhood": match["neighborhood"],
            "city": match["city"],
            "area": target_area,
            "is_listed": is_listed,
            "listing_currency": listing_currency,
            "listing_price": listing_price,
            "structure_count": len(buildings),
            "structure_names": [b.get("buildingName") or b.get("buildingType") or "Unknown"
                                 for b in buildings],
        },
        "upx_valuation": upx_val,
        "usd_valuation": usd_val,
    }
