#!/usr/bin/env python3
"""
Upland Greymass Legacy Backfill

Scans the greymass v1 EOS history API to recover pre-2023 Upland transactions
(genesis 2019-10-11 → 2023-03-18). Fetches all playuplandme actions by position
and filters locally for n2/n4/n5/n52/n111/n112 events.

Uses 3 concurrent HTTP workers + in-order processing to achieve ~3 effective
req/s without triggering Cloudflare rate limits. Estimated runtime: ~20 days.

Results go into the same economy.db as the main scraper — duplicates are
handled by the UNIQUE constraint on trx_id.

Usage:
  python3 greymass_backfill.py             # run (or resume) backfill
  python3 greymass_backfill.py --no-cache  # skip property cache (low RAM — use on Pi)
  python3 greymass_backfill.py --stats     # show progress and exit
"""

import argparse
import gzip
import json
import os
import signal
import sqlite3
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

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

DB_PATH = Path(os.environ.get("ECONOMY_DB", str(ROOT_DIR / "data" / "economy.db")))

GREYMASS_URL = "https://eos.greymass.com/v1/history/get_actions"
HEADERS = {
    "Content-Type": "application/json",
    "User-Agent":   "Mozilla/5.0 (compatible; upland-research/1.0)",
}

# Scan from pos 0 (safe lower bound) to just past 2023-03-18 (~pos 515M).
# Hyperion-based scrapers cover 2023-03-18 onward; duplicates handled by UNIQUE.
START_POS  = 0
END_POS    = 516_000_000

PAGE_SIZE   = 100   # actions per greymass request
WORKERS     = 2     # concurrent HTTP threads (3 triggers Cloudflare rate limits)
WINDOW      = 10    # futures submitted per batch (WORKERS × 5)
INTER_REQ_S = 0.15  # minimum gap between starting each HTTP request
SAVE_EVERY  = 200   # save resume position every N pages (~20k actions)

STATE_KEY  = "greymass_backfill_pos"
DONE_KEY   = "greymass_backfill_done"

RELEVANT   = {"n2", "n4", "n5", "n52", "n111", "n112"}

_stop = False


# ─────────────────────────────────────────────────────────────────────────────
# SQLite helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def get_state(conn, key: str, default=None) -> str:
    row = conn.execute("SELECT value FROM scraper_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO scraper_state(key,value) VALUES(?,?)", (key, value))
    conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Property cache
# ─────────────────────────────────────────────────────────────────────────────

_prop_cache: dict = {}


def load_property_cache() -> None:
    for path in [SCRIPT_DIR / "property_cache.json", SCRIPT_DIR / "property_cache.json.gz"]:
        if not path.exists():
            continue
        try:
            opener = gzip.open if path.suffix == ".gz" else open
            with opener(path, "rt") as f:
                raw = json.load(f)
            for pid, addr in raw.items():
                parts = [p.strip() for p in addr.split(",")]
                _prop_cache[str(pid)] = {
                    "address":      parts[0] if parts else addr,
                    "neighborhood": parts[1] if len(parts) >= 3 else "",
                    "city":         parts[-1] if len(parts) >= 2 else "",
                }
            print(f"[+] Property cache: {len(_prop_cache):,} entries ({path.name})")
            return
        except Exception as e:
            print(f"[!] Cache load failed ({path.name}): {e}")
    print("[!] No property cache — city/neighborhood will be NULL")


def prop_meta(prop_id: str) -> dict:
    return _prop_cache.get(str(prop_id), {"address": None, "neighborhood": None, "city": None})


# ─────────────────────────────────────────────────────────────────────────────
# Greymass v1 API
# ─────────────────────────────────────────────────────────────────────────────

def fetch_page(pos: int) -> tuple:
    """
    Fetch PAGE_SIZE actions starting at pos.
    Returns (pos, actions_list) or (pos, None) on unrecoverable error.
    """
    payload = json.dumps({
        "account_name": "playuplandme",
        "pos":          pos,
        "offset":       PAGE_SIZE - 1,
    }).encode()

    time.sleep(INTER_REQ_S)  # stagger requests to avoid Cloudflare burst detection
    for attempt in range(6):
        try:
            req = urllib.request.Request(GREYMASS_URL, data=payload, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as r:
                return pos, json.loads(r.read()).get("actions", [])
        except urllib.error.HTTPError as e:
            if e.code in (420, 429):
                wait = 60 * (attempt + 1)
                print(f"  [rate limit] pos {pos:,} — sleeping {wait}s", flush=True)
                time.sleep(wait)
            elif e.code == 403:
                wait = 30 * (attempt + 1)
                print(f"  [403] pos {pos:,} — sleeping {wait}s", flush=True)
                time.sleep(wait)
            else:
                time.sleep(5 * (attempt + 1))
        except Exception as e:
            time.sleep(5 * (attempt + 1))

    print(f"  [!] Giving up on pos {pos:,} after 6 attempts", flush=True)
    return pos, None


# ─────────────────────────────────────────────────────────────────────────────
# Price parsing
# ─────────────────────────────────────────────────────────────────────────────

def parse_upx(s) -> float | None:
    if not s:
        return None
    try:
        return float(str(s).replace(" UPX", "").replace(",", "")) or None
    except (ValueError, TypeError):
        return None


def parse_fiat(s) -> float | None:
    if not s:
        return None
    try:
        v = float(str(s).replace(" FIAT", "").replace(",", ""))
        return v if v > 0 else None
    except (ValueError, TypeError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Action processing
# ─────────────────────────────────────────────────────────────────────────────

def _upsert_hourly(conn, timestamp: str, marketplace: str, volume: float) -> None:
    hour = timestamp[:13] + ":00:00"
    conn.execute(
        """INSERT INTO hourly_aggregates(hour,marketplace,trade_count,volume)
           VALUES(?,?,1,?)
           ON CONFLICT(hour,marketplace) DO UPDATE SET
             trade_count=trade_count+1,
             volume=volume+excluded.volume""",
        (hour, marketplace, volume),
    )


def process_page(conn: sqlite3.Connection, raw_actions: list) -> int:
    """
    Process a list of raw greymass v1 actions.
    The greymass format nests action data under action_trace.
    Returns count of rows inserted.
    """
    inserted = 0

    for raw in raw_actions:
        trace = raw.get("action_trace", {})
        act   = trace.get("act", {})
        name  = act.get("name", "")

        if name not in RELEVANT:
            continue

        data      = act.get("data", {})
        trx_id    = trace.get("trx_id", "")
        timestamp = trace.get("block_time", "").rstrip("Z")
        block_num = trace.get("block_num")

        # ── n2 : property listed ─────────────────────────────────────────────
        if name == "n2":
            prop_id = str(data.get("a45", ""))
            fiat    = parse_fiat(data.get("p3", ""))
            if prop_id and fiat:
                conn.execute(
                    "INSERT OR REPLACE INTO pending_usd_listings"
                    "(property_id, seller, usd_price, listed_at) VALUES(?,?,?,?)",
                    (prop_id, data.get("a54"), fiat, timestamp),
                )

        # ── n4 : property unlisted ───────────────────────────────────────────
        elif name == "n4":
            prop_id = str(data.get("a45", ""))
            if prop_id:
                conn.execute(
                    "DELETE FROM pending_usd_listings WHERE property_id=?", (prop_id,)
                )

        # ── n5 : UPX property sale ───────────────────────────────────────────
        elif name == "n5":
            prop_id = str(data.get("a45", ""))
            upx     = parse_upx(data.get("p24", ""))
            buyer   = data.get("p14")
            meta    = prop_meta(prop_id)
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO transactions
                       (trx_id,block_num,timestamp,action,property_id,address,city,neighborhood,
                        buyer,upx_amount,marketplace,asset_type)
                       VALUES(?,?,?,?,?,?,?,?,?,?,'upx','property')""",
                    (trx_id, block_num, timestamp, "n5", prop_id,
                     meta["address"], meta["city"], meta["neighborhood"], buyer, upx),
                )
                if conn.execute("SELECT changes()").fetchone()[0]:
                    inserted += 1
                    _upsert_hourly(conn, timestamp, "upx", upx or 0)
            except Exception as e:
                print(f"  [!] n5 insert: {e}", flush=True)

        # ── n52 : USD property sale ──────────────────────────────────────────
        elif name == "n52":
            prop_id = str(data.get("a45", ""))
            buyer   = data.get("p14")
            meta    = prop_meta(prop_id)
            row = conn.execute(
                "SELECT usd_price, seller FROM pending_usd_listings WHERE property_id=?",
                (prop_id,)
            ).fetchone()
            if row:
                usd_price, seller = row["usd_price"], row["seller"]
                conn.execute(
                    "DELETE FROM pending_usd_listings WHERE property_id=?", (prop_id,)
                )
            else:
                usd_price, seller = None, None
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO transactions
                       (trx_id,block_num,timestamp,action,property_id,address,city,neighborhood,
                        buyer,seller,usd_amount,marketplace,asset_type)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,'usd','property')""",
                    (trx_id, block_num, timestamp, "n52", prop_id,
                     meta["address"], meta["city"], meta["neighborhood"],
                     buyer, seller, usd_price),
                )
                if conn.execute("SELECT changes()").fetchone()[0]:
                    inserted += 1
                    _upsert_hourly(conn, timestamp, "usd", usd_price or 0)
            except Exception as e:
                print(f"  [!] n52 insert: {e}", flush=True)

        # ── n111 / n112 : asset / NFT sales ─────────────────────────────────
        elif name in ("n111", "n112"):
            buyer     = data.get("p1") or data.get("p14")
            seller    = data.get("p2") or data.get("p25")
            raw_price = data.get("p45") or data.get("p141")
            upx       = parse_upx(str(raw_price)) if raw_price else None
            atype     = "asset" if name == "n111" else "nft"
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO transactions
                       (trx_id,block_num,timestamp,action,buyer,seller,
                        upx_amount,marketplace,asset_type)
                       VALUES(?,?,?,?,?,?,?,'upx',?)""",
                    (trx_id, block_num, timestamp, name, buyer, seller, upx, atype),
                )
                if conn.execute("SELECT changes()").fetchone()[0]:
                    inserted += 1
                    _upsert_hourly(conn, timestamp, "upx", upx or 0)
            except Exception as e:
                print(f"  [!] {name} insert: {e}", flush=True)

    conn.commit()
    return inserted


# ─────────────────────────────────────────────────────────────────────────────
# Backfill
# ─────────────────────────────────────────────────────────────────────────────

def backfill(conn: sqlite3.Connection) -> None:
    global _stop

    if get_state(conn, DONE_KEY):
        print("[+] Greymass backfill already complete")
        return

    resume_pos = int(get_state(conn, STATE_KEY, str(START_POS)))
    total_pages = (END_POS - START_POS) // PAGE_SIZE
    done_pages  = (resume_pos - START_POS) // PAGE_SIZE

    print(f"[*] Greymass backfill: pos {resume_pos:,} → {END_POS:,}  ({WORKERS} workers)")
    print(f"    Progress: {done_pages:,} / {total_pages:,} pages  "
          f"({100*done_pages/total_pages:.1f}%)")

    total_inserted = 0
    pages_done     = 0
    t_start        = time.time()

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        pos = resume_pos

        while pos < END_POS and not _stop:
            # Submit a window of futures — in-order iteration ensures sequential DB writes
            batch = [pos + i * PAGE_SIZE for i in range(WINDOW) if pos + i * PAGE_SIZE < END_POS]
            futures = [executor.submit(fetch_page, p) for p in batch]

            for fut in futures:
                if _stop:
                    break
                page_pos, actions = fut.result()

                if actions is None:
                    print(f"  [!] Skipping pos {page_pos:,} (fetch failed)", flush=True)
                    continue

                if actions:
                    n = process_page(conn, actions)
                    total_inserted += n

                pages_done += 1
                last_ts = (
                    actions[-1].get("action_trace", {}).get("block_time", "")[:10]
                    if actions else "?"
                )

                if pages_done % SAVE_EVERY == 0:
                    set_state(conn, STATE_KEY, str(page_pos))
                    elapsed   = time.time() - t_start
                    rate      = pages_done / elapsed if elapsed > 0 else 0
                    remaining = (total_pages - done_pages - pages_done) / rate if rate > 0 else 0
                    print(
                        f"  pos {page_pos:,}  date {last_ts}  "
                        f"inserted {total_inserted:,}  "
                        f"~{remaining/86400:.1f}d remaining",
                        flush=True,
                    )

            pos += WINDOW * PAGE_SIZE
            set_state(conn, STATE_KEY, str(pos))

    if not _stop:
        set_state(conn, DONE_KEY, "1")
        print(f"\n[+] Complete — {total_inserted:,} records inserted")
    else:
        print(f"\n[!] Interrupted at pos {pos:,} — resume will continue from here")


# ─────────────────────────────────────────────────────────────────────────────
# Stats
# ─────────────────────────────────────────────────────────────────────────────

def print_stats(conn: sqlite3.Connection) -> None:
    pos   = int(get_state(conn, STATE_KEY, str(START_POS)))
    done  = get_state(conn, DONE_KEY)
    total_pages = (END_POS - START_POS) // PAGE_SIZE
    done_pages  = (pos - START_POS) // PAGE_SIZE
    pct   = 100 * done_pages / total_pages

    legacy = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE timestamp < '2023-03-18'"
    ).fetchone()[0]

    print(f"""
Greymass Backfill Progress
  Status:     {'COMPLETE' if done else 'in progress'}
  Position:   {pos:,} / {END_POS:,}
  Pages done: {done_pages:,} / {total_pages:,}  ({pct:.1f}%)
  Legacy rows in DB: {legacy:,}
""")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    global _stop

    parser = argparse.ArgumentParser(description="Upland greymass legacy backfill")
    parser.add_argument("--stats",    action="store_true", help="Show progress and exit")
    parser.add_argument("--no-cache", action="store_true", help="Skip property cache (saves ~500MB RAM — use on Pi)")
    args = parser.parse_args()

    if not args.no_cache:
        load_property_cache()
    else:
        print("[*] --no-cache: skipping property cache (address/city/neighborhood will be NULL)")
    conn = get_db()

    if args.stats:
        print_stats(conn)
        return

    def _shutdown(sig, _frame):
        global _stop
        print("\n[!] Shutting down gracefully…")
        _stop = True

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    print(f"[*] DB: {DB_PATH}")
    backfill(conn)
    conn.close()


if __name__ == "__main__":
    main()
