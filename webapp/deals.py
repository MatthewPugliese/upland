"""UPX asks compared with recent neighborhood sales.

Reads the local listings snapshot and stored sales. Does not scan a city.
"""

from __future__ import annotations

from pathlib import Path

from listings_view import build_listings_page

MIN_DISCOUNT_PCT = 10
NEAR_PCT = 25
WIDER_CAP = 40
JUST_OVER_CAP = 20
OVER_SELLER_CAP = 40


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
    from economy import SALE_DOUBLE_PCT
    wider = [
        row for row in page["listings"]
        if row.get("vs_median_pct") is not None
        and NEAR_PCT < row["vs_median_pct"] <= SALE_DOUBLE_PCT
    ]
    wider.sort(key=lambda row: (row["vs_median_pct"], row.get("price") or 0, row.get("address") or ""))
    over_rows = [
        row for row in page["listings"]
        if row.get("vs_median_pct") is not None and row["vs_median_pct"] > SALE_DOUBLE_PCT
    ]
    over_rows.sort(key=lambda row: (row["vs_median_pct"], row.get("price") or 0, row.get("address") or ""))
    over_groups: dict = {}
    for row in over_rows:
        name = (row.get("owner_username") or "").strip()
        slot = over_groups.setdefault(name.casefold(), {
            "owner_username": name,
            "asks": 0,
            "closest_pct": None,
            "closest_address": "",
            "closest_id": "",
            "closest_neighborhood": "",
            "closest_price": None,
        })
        slot["asks"] += 1
        pct = row["vs_median_pct"]
        if slot["closest_pct"] is None or (pct, row.get("price") or 0) < (
            slot["closest_pct"], slot["closest_price"] or 0
        ):
            slot["closest_pct"] = pct
            slot["closest_address"] = row.get("address") or ""
            slot["closest_id"] = row.get("id") or ""
            slot["closest_neighborhood"] = row.get("neighborhood") or ""
            slot["closest_price"] = row.get("price")
    over_sellers = sorted(
        over_groups.values(),
        key=lambda row: (-row["asks"], row["closest_pct"], row["owner_username"].casefold()),
    )
    repeat_sellers = [row for row in over_sellers if row["asks"] >= 2]
    summary = page.get("deals") or {}
    return {
        "db_exists": page["db_exists"],
        "deals": deals,
        "matches": matches[:20],
        "near": near[:30],
        "wider": wider[:WIDER_CAP],
        "wider_count": len(wider),
        "over_double": len(over_rows),
        "just_over": over_rows[:JUST_OVER_CAP],
        "over_sellers": repeat_sellers[:OVER_SELLER_CAP],
        "over_seller_count": len(over_sellers),
        "over_repeat_count": len(repeat_sellers),
        "over_single_asks": sum(row["asks"] for row in over_sellers if row["asks"] == 1),
        "near_pct": NEAR_PCT,
        "double_pct": SALE_DOUBLE_PCT,
        "checked": summary.get("compared", 0),
        "under_median": summary.get("under_median", 0),
        "at_median": summary.get("at_median", 0),
        "window_days": summary.get("window_days", 90),
        "min_discount_pct": min_discount_pct,
        "stored_upx": page["summary"]["upx"] if page["db_exists"] else 0,
    }
