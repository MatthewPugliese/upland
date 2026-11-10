"""Read-only view model for the for-sale snapshot page.

Does not create the database. An empty snapshot is a normal state: the
crawler has not been run on this machine yet.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRAPER = Path(__file__).resolve().parent.parent / "scraper"
if str(_SCRAPER) not in sys.path:
    sys.path.insert(0, str(_SCRAPER))

from listings_db import (  # noqa: E402
    DEFAULT_DB,
    connect,
    coverage_rows,
    distinct_values,
    search_listings,
    summary,
)


def build_listings_page(args, db_path: Path | None = None) -> dict:
    """args is a mapping (request.args or a plain dict)."""
    currency = (args.get("currency") or "UPX").strip()
    city = (args.get("city") or "").strip()
    neighborhood = (args.get("neighborhood") or "").strip()
    owner = (args.get("owner") or "").strip()
    sort = args.get("sort") or "markup"
    if sort not in {"markup", "price", "seen", "deal"}:
        sort = "markup"
    try:
        limit = int(args.get("limit") or 100)
    except (TypeError, ValueError):
        limit = 100
    limit = min(max(limit, 1), 500)

    filters = {
        "currency": currency or "UPX",
        "city": city,
        "neighborhood": neighborhood,
        "owner": owner,
        "sort": sort,
        "limit": limit,
    }
    path = Path(db_path) if db_path else DEFAULT_DB
    empty = {
        "db_exists": False,
        "filters": filters,
        "listings": [],
        "cities": [],
        "neighborhoods": [],
        "coverage": [],
        "summary": {
            "listings": 0, "upx": 0, "usd": 0,
            "freshest_listing": None, "freshest_crawl": None, "targets": {},
        },
        "deals": {"compared": 0, "under_median": 0, "at_median": 0, "window_days": 90},
        "sale_context": None,
    }
    if not path.exists():
        return empty

    conn = connect(path)
    try:
        info = summary(conn)
        listings = search_listings(
            conn,
            currency=None if filters["currency"].lower() == "any" else filters["currency"].upper(),
            city=city or None,
            neighborhood=neighborhood or None,
            owner=owner or None,
            sort="markup" if sort == "deal" else sort,
            limit=limit,
        )
        deals = _annotate_deals(listings)
        sale_context = None
        if neighborhood:
            for row in listings:
                if row.get("sale_median_upx"):
                    sale_context = {
                        "median_upx": row["sale_median_upx"],
                        "sales": row["sale_count"],
                        "window_days": deals.get("window_days", 90),
                    }
                    break
        if sort == "deal":
            listings.sort(key=lambda row: (
                row.get("vs_median_pct") is None,
                row.get("vs_median_pct") if row.get("vs_median_pct") is not None else 0,
            ))
        return {
            "db_exists": True,
            "filters": filters,
            "listings": listings,
            "cities": distinct_values(conn, "city"),
            "neighborhoods": distinct_values(conn, "neighborhood"),
            "coverage": coverage_rows(conn),
            "summary": info,
            "deals": deals,
            "sale_context": sale_context,
        }
    finally:
        conn.close()


def _annotate_deals(listings: list) -> dict:
    """Mark each UPX ask against the recent neighborhood sale median."""
    deals = {"compared": 0, "under_median": 0, "at_median": 0, "window_days": 90}
    try:
        from economy import neighborhood_sale_medians
        medians = neighborhood_sale_medians([row.get("neighborhood") or "" for row in listings])
    except Exception:
        medians = {}
    folded = {name.casefold(): info for name, info in medians.items()}
    for row in listings:
        info = folded.get((row.get("neighborhood") or "").casefold())
        price = row.get("price_upx")
        median = info.get("median_upx") if info else None
        if not info or not price or not median:
            row["sale_median_upx"] = None
            row["vs_median_pct"] = None
            row["sale_count"] = None
            continue
        row["sale_median_upx"] = median
        row["sale_count"] = info["sales"]
        row["vs_median_pct"] = round(100 * (price - median) / median, 1)
        deals["compared"] += 1
        if row["vs_median_pct"] < 0:
            deals["under_median"] += 1
        elif row["vs_median_pct"] == 0:
            deals["at_median"] += 1
    return deals
