"""Write data/size_rates.json from cached lot sizes and an exported sale list.

The sale list is a JSON array of {property_id, timestamp, upx_amount}. Build it
with the property-id index on economy.db. This script does not open that
database, and the listings page only reads the file this writes.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import valuation

ROOT = Path(__file__).resolve().parent.parent
PROP_CACHE = ROOT / "scraper" / "property_cache.db"
DIMS = ROOT / "optimizer" / "cache" / "Dongan_Hills_api_dims_cache.json"
OUT = valuation.SIZE_RATES_PATH


def _lookup(conn: sqlite3.Connection, prop_ids: list[str]) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for start in range(0, len(prop_ids), 800):
        chunk = prop_ids[start:start + 800]
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT prop_id, neighborhood, city FROM properties WHERE prop_id IN ({marks})",
            chunk,
        ).fetchall()
        for prop_id, neighborhood, city in rows:
            found[str(prop_id)] = {
                "neighborhood": neighborhood or "",
                "city": city or "",
            }
    return found


def sized_properties() -> dict[str, dict]:
    """Property ids whose UP² is already cached, with neighborhood and city."""
    sized: dict[str, dict] = {}
    for prop_id, entry in valuation._load_area_cache().items():
        if isinstance(entry, dict) and entry.get("area"):
            sized[str(prop_id)] = {
                "area": float(entry["area"]),
                "neighborhood": "",
                "city": "",
            }
    if PROP_CACHE.exists() and sized:
        conn = sqlite3.connect(f"file:{PROP_CACHE}?mode=ro", uri=True)
        try:
            places = _lookup(conn, list(sized))
        finally:
            conn.close()
        for prop_id, place in places.items():
            sized[prop_id].update(place)
    if DIMS.exists() and PROP_CACHE.exists():
        raw = json.loads(DIMS.read_text())
        by_address = {
            key.upper().strip(): float(value["up2"])
            for key, value in raw.items()
            if key != "_ts" and isinstance(value, dict) and value.get("up2")
        }
        conn = sqlite3.connect(f"file:{PROP_CACHE}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT prop_id, address, neighborhood, city FROM properties "
                "WHERE neighborhood = ?",
                ("DONGAN HILLS",),
            ).fetchall()
        finally:
            conn.close()
        for prop_id, address, neighborhood, city in rows:
            area = by_address.get((address or "").upper().strip())
            if not area:
                continue
            current = sized.get(str(prop_id))
            if current and current.get("area"):
                current["neighborhood"] = current["neighborhood"] or neighborhood or ""
                current["city"] = current["city"] or city or ""
                continue
            sized[str(prop_id)] = {
                "area": area,
                "neighborhood": neighborhood or "",
                "city": city or "",
            }
    return sized


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(f"usage: python3 {Path(__file__).name} sales.json")
    sales = json.loads(Path(sys.argv[1]).read_text())
    table = valuation.rates_from_sales(
        sales, sized_properties(), now=datetime.now(timezone.utc),
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(table))
    print(
        f"wrote {OUT} neighborhoods {len(table['neighborhoods'])} "
        f"cities {len(table['cities'])} from {len(sales)} sales"
    )


if __name__ == "__main__":
    main()
