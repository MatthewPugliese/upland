"""Sellers in the stored UPX snapshot, compared with recent sales.

Reads the local listings snapshot and stored sales. Does not scan a city.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRAPER = Path(__file__).resolve().parent.parent / "scraper"
if str(_SCRAPER) not in sys.path:
    sys.path.insert(0, str(_SCRAPER))

from listings_db import DEFAULT_DB, connect, summary  # noqa: E402

MIN_ASKS = 2
WINDOW_DAYS = 90


def build_sellers(db_path: Path | None = None, min_asks: int = MIN_ASKS) -> dict:
    path = Path(db_path) if db_path else DEFAULT_DB
    page = {
        "db_exists": False,
        "sellers": [],
        "min_asks": min_asks,
        "window_days": WINDOW_DAYS,
        "stored_upx": 0,
        "freshest_listing": None,
        "skipped_single": 0,
    }
    if not path.exists():
        return page

    conn = connect(path)
    try:
        snap = summary(conn)
        rows = conn.execute(
            """
            SELECT property_id, address, owner_username, neighborhood, city, price
            FROM current_listings
            WHERE currency = 'UPX' AND price > 0
            """
        ).fetchall()
    finally:
        conn.close()

    page["db_exists"] = True
    page["stored_upx"] = snap["upx"]
    page["freshest_listing"] = snap["freshest_listing"]

    groups: dict = {}
    for row in rows:
        name = (row["owner_username"] or "").strip()
        fold = name.casefold()
        bucket = groups.setdefault(fold, {
            "owner_username": name,
            "asks": [],
            "neighborhoods": {},
        })
        if name and not bucket["owner_username"]:
            bucket["owner_username"] = name
        neighborhood = (row["neighborhood"] or "").strip()
        bucket["asks"].append({
            "id": row["property_id"],
            "address": row["address"] or "",
            "price": row["price"],
            "neighborhood": neighborhood,
            "city": (row["city"] or "").strip(),
        })
        if neighborhood:
            bucket["neighborhoods"][neighborhood.casefold()] = neighborhood

    ready = []
    for bucket in groups.values():
        if len(bucket["asks"]) < min_asks:
            page["skipped_single"] += 1
            continue
        ready.append(bucket)
    if not ready:
        return page

    import economy
    medians = economy.neighborhood_sale_medians(
        [name for bucket in ready for name in bucket["neighborhoods"].values()],
        days=WINDOW_DAYS,
    )
    folded = {name.casefold(): info for name, info in medians.items()}

    sellers = []
    for bucket in ready:
        compared = []
        for ask in bucket["asks"]:
            info = folded.get(ask["neighborhood"].casefold()) if ask["neighborhood"] else None
            median = info.get("median_upx") if info else None
            if not median:
                continue
            ask["vs_median_pct"] = round(100 * (ask["price"] - median) / median, 1)
            ask["sale_median_upx"] = median
            compared.append(ask)
        best = min(compared, key=lambda ask: ask["vs_median_pct"]) if compared else None
        lowest = min(bucket["asks"], key=lambda ask: ask["price"])
        shown = best or lowest
        neighborhoods = sorted(bucket["neighborhoods"].values(), key=str.casefold)
        counts = economy.sale_distance_counts([ask["vs_median_pct"] for ask in compared])
        sellers.append({
            "owner_username": bucket["owner_username"],
            "asks": len(bucket["asks"]),
            "compared": len(compared),
            "under_median": sum(1 for ask in compared if ask["vs_median_pct"] < 0),
            "at_or_under": counts["at_or_under"],
            "near_over": counts["near_over"],
            "up_to_double": counts["up_to_double"],
            "over_double": counts["over_double"],
            "best_vs_median_pct": best["vs_median_pct"] if best else None,
            "lowest_ask": lowest["price"],
            "best_address": shown["address"],
            "best_id": shown["id"],
            "best_neighborhood": shown["neighborhood"],
            "neighborhoods": neighborhoods,
        })

    sellers.sort(key=lambda row: (
        row["best_vs_median_pct"] is None,
        row["best_vs_median_pct"] if row["best_vs_median_pct"] is not None else 0,
        -row["asks"],
        row["owner_username"].casefold(),
    ))
    page["sellers"] = sellers
    return page
