"""Stored UPX asks rolled up by city and compared with recent sales.

Reads the local listings snapshot and stored sales. Does not scan a city.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRAPER = Path(__file__).resolve().parent.parent / "scraper"
if str(_SCRAPER) not in sys.path:
    sys.path.insert(0, str(_SCRAPER))

from listings_db import DEFAULT_DB, connect, summary  # noqa: E402

WINDOW_DAYS = 90
NEAR_PCT = 25
MAX_NEIGHBORHOODS = 400


def _median(amounts: list) -> float:
    ordered = sorted(amounts)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def build_cities(db_path: Path | None = None) -> dict:
    path = Path(db_path) if db_path else DEFAULT_DB
    page = {
        "db_exists": False,
        "cities": [],
        "window_days": WINDOW_DAYS,
        "near_pct": NEAR_PCT,
        "freshest_listing": None,
        "stored_upx": 0,
    }
    if not path.exists():
        return page

    conn = connect(path)
    try:
        snap = summary(conn)
        rows = conn.execute(
            """
            SELECT property_id, address, owner_username, neighborhood, city,
                   currency, price
            FROM current_listings
            WHERE price > 0 AND city IS NOT NULL AND trim(city) != ''
            """
        ).fetchall()
    finally:
        conn.close()

    page["db_exists"] = True
    page["freshest_listing"] = snap["freshest_listing"]
    page["stored_upx"] = snap["upx"]

    groups: dict = {}
    hood_asks: dict = {}
    for row in rows:
        city = (row["city"] or "").strip()
        fold = city.casefold()
        bucket = groups.setdefault(fold, {
            "city": city,
            "spellings": {},
            "upx": [],
            "usd": 0,
        })
        bucket["spellings"][city] = bucket["spellings"].get(city, 0) + 1
        if (row["currency"] or "").upper() == "UPX":
            neighborhood = (row["neighborhood"] or "").strip()
            bucket["upx"].append({
                "id": row["property_id"],
                "address": row["address"] or "",
                "price": row["price"],
                "neighborhood": neighborhood,
                "owner_username": (row["owner_username"] or "").strip(),
            })
            if neighborhood:
                hood_asks[neighborhood.casefold()] = hood_asks.get(neighborhood.casefold(), 0) + 1
        elif (row["currency"] or "").upper() == "USD":
            bucket["usd"] += 1

    spellings = {}
    for row in rows:
        neighborhood = (row["neighborhood"] or "").strip()
        if not neighborhood or (row["currency"] or "").upper() != "UPX":
            continue
        fold = neighborhood.casefold()
        spellings.setdefault(fold, {})
        spellings[fold][neighborhood] = spellings[fold].get(neighborhood, 0) + 1
    ordered = sorted(spellings, key=lambda fold: (-hood_asks.get(fold, 0), fold))[:MAX_NEIGHBORHOODS]
    hood_names = [
        max(spellings[fold], key=spellings[fold].get) for fold in ordered
    ]

    medians = {}
    if hood_names:
        import economy
        medians = economy.neighborhood_sale_medians(hood_names, days=WINDOW_DAYS)
    folded = {name.casefold(): info for name, info in medians.items()}

    cities = []
    for bucket in groups.values():
        prices = [ask["price"] for ask in bucket["upx"]]
        compared = []
        skipped = {}
        for ask in bucket["upx"]:
            info = folded.get(ask["neighborhood"].casefold()) if ask["neighborhood"] else None
            median = info.get("median_upx") if info else None
            if not median:
                label = ask["neighborhood"] or "No neighborhood"
                slot = skipped.setdefault(label.casefold(), {"spellings": {}, "asks": 0})
                slot["spellings"][label] = slot["spellings"].get(label, 0) + 1
                slot["asks"] += 1
                continue
            ask = dict(ask)
            ask["vs_median_pct"] = round(100 * (ask["price"] - median) / median, 1)
            ask["sale_median_upx"] = median
            compared.append(ask)
        uncompared = sorted(
            (
                {"neighborhood": max(slot["spellings"], key=slot["spellings"].get), "asks": slot["asks"]}
                for slot in skipped.values()
            ),
            key=lambda row: (-row["asks"], row["neighborhood"].casefold()),
        )
        best = min(compared, key=lambda ask: (ask["vs_median_pct"], ask["price"])) if compared else None
        city_name = max(bucket["spellings"], key=bucket["spellings"].get)
        import economy
        counts = economy.sale_distance_counts([ask["vs_median_pct"] for ask in compared])
        cities.append({
            "city": city_name,
            "upx_asks": len(prices),
            "usd_asks": bucket["usd"],
            "lowest_ask": min(prices) if prices else None,
            "median_ask": _median(prices) if prices else None,
            "compared": len(compared),
            "within_near": counts["within_near"],
            "at_or_under": counts["at_or_under"],
            "near_over": counts["near_over"],
            "up_to_double": counts["up_to_double"],
            "over_double": counts["over_double"],
            "uncompared": uncompared,
            "closest_pct": best["vs_median_pct"] if best else None,
            "closest_address": best["address"] if best else "",
            "closest_id": best["id"] if best else "",
            "closest_neighborhood": best["neighborhood"] if best else "",
            "closest_price": best["price"] if best else None,
        })

    cities.sort(key=lambda row: (
        row["closest_pct"] is None,
        row["closest_pct"] if row["closest_pct"] is not None else 0,
        -row["upx_asks"],
        row["city"].casefold(),
    ))
    page["cities"] = cities
    return page
