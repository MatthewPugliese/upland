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
    sort = args.get("sort") or "markup"
    if sort not in {"markup", "price", "seen"}:
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
            sort=sort,
            limit=limit,
        )
        return {
            "db_exists": True,
            "filters": filters,
            "listings": listings,
            "cities": distinct_values(conn, "city"),
            "neighborhoods": distinct_values(conn, "neighborhood"),
            "coverage": coverage_rows(conn),
            "summary": info,
        }
    finally:
        conn.close()
