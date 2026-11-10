"""UPX asks compared with recent neighborhood sales.

Reads the local listings snapshot and stored sales. Does not scan a city.
"""

from __future__ import annotations

from pathlib import Path

from listings_view import build_listings_page

MIN_DISCOUNT_PCT = 10
NEAR_PCT = 25


def build_deals(db_path: Path | None = None, limit: int = 500,
                min_discount_pct: float = MIN_DISCOUNT_PCT) -> dict:
    page = build_listings_page(
        {"currency": "UPX", "sort": "price", "limit": limit},
        db_path=db_path,
    )
    deals = [
        row for row in page["listings"]
        if row.get("vs_median_pct") is not None and row["vs_median_pct"] <= -min_discount_pct
    ]
    deals.sort(key=lambda row: row["vs_median_pct"])
    matches = [row for row in page["listings"] if row.get("vs_median_pct") == 0]
    matches.sort(key=lambda row: (row.get("price") or 0, row.get("address") or ""))
    near = [
        row for row in page["listings"]
        if row.get("vs_median_pct") is not None
        and row["vs_median_pct"] != 0
        and -min_discount_pct < row["vs_median_pct"] <= NEAR_PCT
    ]
    near.sort(key=lambda row: (abs(row["vs_median_pct"]), row["vs_median_pct"], row.get("price") or 0))
    summary = page.get("deals") or {}
    return {
        "db_exists": page["db_exists"],
        "deals": deals,
        "matches": matches[:20],
        "near": near[:30],
        "near_pct": NEAR_PCT,
        "checked": summary.get("compared", 0),
        "under_median": summary.get("under_median", 0),
        "at_median": summary.get("at_median", 0),
        "window_days": summary.get("window_days", 90),
        "min_discount_pct": min_discount_pct,
        "stored_upx": page["summary"]["upx"] if page["db_exists"] else 0,
    }
