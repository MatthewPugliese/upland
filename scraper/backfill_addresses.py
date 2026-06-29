#!/usr/bin/env python3
"""
Backfill missing address/city/neighborhood for property sales where the
property_id wasn't in the local cache at scrape time.

Calls the Upland Developers API for each unknown property ID and updates
the transactions table in-place. Saves progress to scraper_state so it
can be interrupted and resumed safely.

Usage:
  python3 scraper/backfill_addresses.py
  python3 scraper/backfill_addresses.py --rate 5   # requests per second (default 5)
  python3 scraper/backfill_addresses.py --dry-run  # count only, no API calls
"""

import argparse
import base64
import json
import os
import sqlite3
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR   = SCRIPT_DIR.parent

for _env in [ROOT_DIR / ".env", SCRIPT_DIR / ".env"]:
    if _env.exists():
        with open(_env) as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line or _line.startswith("#") or "=" not in _line:
                    continue
                k, _, v = _line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        break

DB_PATH        = Path(os.environ.get("ECONOMY_DB", str(ROOT_DIR / "data" / "economy.db")))
UPLAND_APP_ID  = os.environ.get("UPLAND_APP_ID", "")
UPLAND_SECRET  = os.environ.get("UPLAND_SECRET", "")
UPLAND_API_URL = "https://api.prod.upland.me/developers-api"
STATE_KEY      = "backfill_addr_last_id"


def auth_headers():
    creds = base64.b64encode(f"{UPLAND_APP_ID}:{UPLAND_SECRET}".encode()).decode()
    return {"Authorization": f"Basic {creds}", "Accept": "application/json", "User-Agent": "Mozilla/5.0"}


def lookup(prop_id: str) -> dict | None:
    url = f"{UPLAND_API_URL}/properties/{prop_id}"
    req = urllib.request.Request(url, headers=auth_headers())
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
        return {
            "address":      d.get("address"),
            "neighborhood": (d.get("neighborhood") or {}).get("name"),
            "city":         (d.get("city") or {}).get("name"),
        }
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"address": None, "neighborhood": None, "city": None}
        raise
    except Exception:
        return None  # transient error — caller will retry


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rate",    type=float, default=5.0, help="API requests per second")
    parser.add_argument("--dry-run", action="store_true",     help="Count missing rows, no API calls")
    args = parser.parse_args()

    if not UPLAND_APP_ID or not UPLAND_SECRET:
        print("[!] UPLAND_APP_ID / UPLAND_SECRET not set — check .env")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    total_missing = conn.execute(
        "SELECT COUNT(DISTINCT property_id) FROM transactions "
        "WHERE asset_type='property' AND address IS NULL AND property_id IS NOT NULL"
    ).fetchone()[0]

    print(f"[*] Properties with missing address: {total_missing:,}")

    if args.dry_run or total_missing == 0:
        conn.close()
        return

    # Resume from last saved progress
    row = conn.execute("SELECT value FROM scraper_state WHERE key=?", (STATE_KEY,)).fetchone()
    last_done = int(row["value"]) if row else 0
    print(f"[*] Resuming from property_id cursor: {last_done}")
    print(f"[*] Rate: {args.rate}/s  (~{total_missing/args.rate/3600:.1f}h to complete)")
    print()

    # Pull distinct missing IDs in ascending order for stable pagination
    ids = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT property_id FROM transactions "
            "WHERE asset_type='property' AND address IS NULL AND property_id IS NOT NULL "
            "AND CAST(property_id AS INTEGER) > ? "
            "ORDER BY CAST(property_id AS INTEGER) ASC",
            (last_done,)
        ).fetchall()
    ]

    sleep_s   = 1.0 / args.rate
    done      = 0
    found     = 0
    not_found = 0
    errors    = 0
    start     = time.monotonic()

    for prop_id in ids:
        meta = None
        for attempt in range(3):
            meta = lookup(prop_id)
            if meta is not None:
                break
            time.sleep(2 ** attempt)

        if meta is None:
            errors += 1
        elif meta["address"]:
            conn.execute(
                "UPDATE transactions SET address=?, city=?, neighborhood=? "
                "WHERE property_id=? AND address IS NULL",
                (meta["address"], meta["city"], meta["neighborhood"], prop_id)
            )
            conn.execute(
                "INSERT OR REPLACE INTO scraper_state(key,value) VALUES(?,?)",
                (STATE_KEY, str(prop_id))
            )
            conn.commit()
            found += 1
        else:
            not_found += 1

        done += 1
        if done % 100 == 0:
            elapsed   = time.monotonic() - start
            remaining = (len(ids) - done) / args.rate
            print(
                f"  {done:>6}/{len(ids)}  "
                f"found={found}  not_found={not_found}  errors={errors}  "
                f"~{remaining/3600:.1f}h left"
            )

        time.sleep(sleep_s)

    elapsed = time.monotonic() - start
    print(f"\n[+] Done — {found} updated, {not_found} not found, {errors} errors  ({elapsed/60:.1f} min)")
    conn.close()


if __name__ == "__main__":
    main()
