#!/usr/bin/env python3
"""
Convert property_cache.json → property_cache.db (SQLite).

A SQLite property cache lets scrapers look up addresses by prop_id without
loading all 4.7M entries into RAM — each lookup reads only the needed pages.

Usage:
  python3 build_property_db.py
"""

import gzip
import json
import sqlite3
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
JSON_PATH  = SCRIPT_DIR / "property_cache.json"
GZ_PATH    = SCRIPT_DIR / "property_cache.json.gz"
DB_PATH    = SCRIPT_DIR / "property_cache.db"


def main():
    # Load JSON
    if JSON_PATH.exists():
        src = JSON_PATH
        opener = open
    elif GZ_PATH.exists():
        src = GZ_PATH
        opener = gzip.open
    else:
        print("[!] No property_cache.json or .json.gz found")
        return

    print(f"[*] Loading {src.name}…")
    with opener(src, "rt") as f:
        raw = json.load(f)
    print(f"[+] {len(raw):,} entries loaded")

    # Build SQLite
    if DB_PATH.exists():
        DB_PATH.unlink()

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")  # safe here — build-once operation
    conn.execute("""
        CREATE TABLE properties (
            prop_id      TEXT PRIMARY KEY,
            address      TEXT,
            neighborhood TEXT,
            city         TEXT
        )
    """)

    batch = []
    for prop_id, addr in raw.items():
        parts = [p.strip() for p in addr.split(",")]
        if len(parts) >= 3:
            address      = parts[0]
            neighborhood = parts[1]
            city         = parts[-1]
        elif len(parts) == 2:
            address      = parts[0]
            neighborhood = None
            city         = parts[1]
        else:
            address      = addr.strip()
            neighborhood = None
            city         = None
        batch.append((str(prop_id), address, neighborhood, city))

        if len(batch) >= 50_000:
            conn.executemany(
                "INSERT OR REPLACE INTO properties VALUES(?,?,?,?)", batch
            )
            conn.commit()
            batch.clear()

    if batch:
        conn.executemany("INSERT OR REPLACE INTO properties VALUES(?,?,?,?)", batch)
        conn.commit()

    count = conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0]
    conn.close()

    size_mb = DB_PATH.stat().st_size / 1_048_576
    print(f"[+] Done — {count:,} rows, {size_mb:.1f} MB → {DB_PATH.name}")


if __name__ == "__main__":
    main()
