"""Neighborhood UPX asks compared with recent sale medians.

Reads the local listings snapshot and stored sales. Does not scan a city.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRAPER = Path(__file__).resolve().parent.parent / "scraper"
if str(_SCRAPER) not in sys.path:
    sys.path.insert(0, str(_SCRAPER))

from listings_db import DEFAULT_DB, connect, summary  # noqa: E402

MIN_ASKS = 3
WINDOW_DAYS = 90
MAX_NEIGHBORHOODS = 400


def _median(amounts: list) -> float:
    ordered = sorted(amounts)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def build_markets(db_path: Path | None = None, min_asks: int = MIN_ASKS) -> dict:
    path = Path(db_path) if db_path else DEFAULT_DB
    page = {
        "db_exists": False,
        "markets": [],
        "min_asks": min_asks,
        "window_days": WINDOW_DAYS,
        "neighborhoods_with_asks": 0,
        "skipped_thin": 0,
        "no_sales": 0,
        "omitted_for_cap": 0,
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
            SELECT neighborhood, city, price
            FROM current_listings
            WHERE currency = 'UPX' AND price > 0
              AND neighborhood IS NOT NULL AND trim(neighborhood) != ''
            """
        ).fetchall()
    finally:
        conn.close()

    page["db_exists"] = True
    page["freshest_listing"] = snap["freshest_listing"]
    page["stored_upx"] = snap["upx"]

    groups: dict = {}
    for row in rows:
        fold = row["neighborhood"].casefold()
        bucket = groups.setdefault(fold, {
            "neighborhood": row["neighborhood"],
            "cities": {},
            "prices": [],
        })
        bucket["prices"].append(row["price"])
        city = (row["city"] or "").strip() or "—"
        bucket["cities"][city] = bucket["cities"].get(city, 0) + 1

    page["neighborhoods_with_asks"] = len(groups)
    candidates = []
    for bucket in groups.values():
        if len(bucket["prices"]) < min_asks:
            page["skipped_thin"] += 1
            continue
        city = max(bucket["cities"].items(), key=lambda item: item[1])[0]
        prices = bucket["prices"]
        candidates.append({
            "neighborhood": bucket["neighborhood"],
            "city": city,
            "asks": len(prices),
            "median_ask": _median(prices),
            "lowest_ask": min(prices),
        })

    candidates.sort(key=lambda row: (-row["asks"], row["neighborhood"].casefold()))
    chosen = candidates[:MAX_NEIGHBORHOODS]
    page["omitted_for_cap"] = len(candidates) - len(chosen)
    if not chosen:
        return page

    import economy
    medians = economy.neighborhood_sale_medians(
        [row["neighborhood"] for row in chosen],
        days=WINDOW_DAYS,
    )
    folded = {name.casefold(): info for name, info in medians.items()}

    markets = []
    for row in chosen:
        info = folded.get(row["neighborhood"].casefold())
        median = info.get("median_upx") if info else None
        if not median:
            page["no_sales"] += 1
            continue
        row["sale_median_upx"] = median
        row["sales"] = info["sales"]
        row["median_vs_sales_pct"] = round(100 * (row["median_ask"] - median) / median, 1)
        row["floor_vs_sales_pct"] = round(100 * (row["lowest_ask"] - median) / median, 1)
        markets.append(row)

    markets.sort(key=lambda row: (row["floor_vs_sales_pct"], row["neighborhood"].casefold()))
    page["markets"] = markets
    return page
