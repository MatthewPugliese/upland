#!/usr/bin/env python3
"""
Upland Economy Scraper

Scrapes both the EOS mainchain and Upland AppChain for sale events and writes
them to a local SQLite database. On first run, backfills all available history
(EOS: Mar 2023 → Apr 2025, AppChain: Apr 2025 → present). On subsequent runs,
resumes from where it left off and polls live.

Handles:
  n2   — property listed (UPX or USD price)
  n4   — property unlisted
  n5   — UPX property sale
  n52  — USD property sale (price recovered via pending_usd_listings cache)
  n111 — UPX asset/spark sale
  n112 — UPX NFT/dGoods sale

Usage:
  python3 economy_scraper.py               # backfill + live daemon
  python3 economy_scraper.py --backfill-only
  python3 economy_scraper.py --live-only
  python3 economy_scraper.py --stats
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
from datetime import datetime, timezone
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent

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

PROPERTY_CACHE_CANDIDATES = [
    SCRIPT_DIR / "property_cache.json",
    SCRIPT_DIR / "property_cache.json.gz",
]

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

UPLAND_APP_ID  = os.environ.get("UPLAND_APP_ID", "")
UPLAND_SECRET  = os.environ.get("UPLAND_SECRET", "")
UPLAND_API_URL = "https://api.prod.upland.me/developers-api"

APPCHAIN_URL = "https://chain-history.upland.me"
EOS_URL      = "https://eos.hyperion.eosrio.io"

# Earliest confirmed data per chain
EOS_START      = "2023-03-18T00:00:00.000"
APPCHAIN_START = "2025-04-28T00:00:00.000"

SALE_ACTIONS    = {"n5", "n52", "n111", "n112"}
LISTING_ACTIONS = {"n2", "n4"}
ALL_ACTIONS     = SALE_ACTIONS | LISTING_ACTIONS
_FILTER         = "playuplandme:" + ",playuplandme:".join(sorted(ALL_ACTIONS))

POLL_INTERVAL = 5    # seconds between live polls
BATCH_SIZE    = 100  # actions per API request
BACKFILL_SLEEP = 0.3 # seconds between backfill requests

# ─────────────────────────────────────────────────────────────────────────────
# Property cache (prop_id → {address, neighborhood, city})
# ─────────────────────────────────────────────────────────────────────────────

_prop_cache: dict = {}
_live_lookup_enabled: bool = False


def _api_auth_headers() -> dict:
    import base64
    creds = base64.b64encode(f"{UPLAND_APP_ID}:{UPLAND_SECRET}".encode()).decode()
    return {"Authorization": f"Basic {creds}", "Accept": "application/json", "User-Agent": "Mozilla/5.0"}


def _api_lookup_property(prop_id: str) -> dict:
    url = f"{UPLAND_API_URL}/properties/{prop_id}"
    req = urllib.request.Request(url, headers=_api_auth_headers())
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
        return {
            "address":      d.get("address"),
            "neighborhood": (d.get("neighborhood") or {}).get("name"),
            "city":         (d.get("city") or {}).get("name"),
        }
    except Exception:
        return {"address": None, "neighborhood": None, "city": None}


def load_property_cache() -> None:
    for path in PROPERTY_CACHE_CANDIDATES:
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
    key = str(prop_id)
    if key in _prop_cache:
        return _prop_cache[key]
    if _live_lookup_enabled and UPLAND_APP_ID and UPLAND_SECRET:
        meta = _api_lookup_property(key)
        _prop_cache[key] = meta
        return meta
    return {"address": None, "neighborhood": None, "city": None}


# ─────────────────────────────────────────────────────────────────────────────
# SQLite
# ─────────────────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS transactions (
            id           INTEGER PRIMARY KEY,
            trx_id       TEXT UNIQUE NOT NULL,
            block_num    INTEGER,
            timestamp    TEXT NOT NULL,
            action       TEXT NOT NULL,
            property_id  TEXT,
            address      TEXT,
            city         TEXT,
            neighborhood TEXT,
            buyer        TEXT,
            seller       TEXT,
            upx_amount   REAL,
            usd_amount   REAL,
            marketplace  TEXT NOT NULL,
            asset_type   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tx_ts     ON transactions(timestamp);
        CREATE INDEX IF NOT EXISTS idx_tx_city   ON transactions(city);
        CREATE INDEX IF NOT EXISTS idx_tx_action ON transactions(action);
        CREATE INDEX IF NOT EXISTS idx_tx_prop   ON transactions(property_id);

        CREATE TABLE IF NOT EXISTS pending_usd_listings (
            property_id TEXT PRIMARY KEY,
            seller      TEXT,
            usd_price   REAL NOT NULL,
            listed_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS hourly_aggregates (
            hour        TEXT NOT NULL,
            marketplace TEXT NOT NULL,
            trade_count INTEGER NOT NULL DEFAULT 0,
            volume      REAL    NOT NULL DEFAULT 0,
            PRIMARY KEY (hour, marketplace)
        );

        CREATE TABLE IF NOT EXISTS scraper_state (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Scraper state (resume cursors)
# ─────────────────────────────────────────────────────────────────────────────

def get_state(conn, key: str, default: str = None) -> str:
    row = conn.execute("SELECT value FROM scraper_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO scraper_state(key,value) VALUES(?,?)", (key, value)
    )
    conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Hyperion API fetch
# ─────────────────────────────────────────────────────────────────────────────

def fetch_actions(base_url: str, after: str, before: str = None,
                  limit: int = BATCH_SIZE, retries: int = 3) -> list:
    params = [
        f"account=playuplandme",
        f"filter={_FILTER}",
        f"limit={limit}",
        f"sort=asc",
        f"after={after}",
    ]
    if before:
        params.append(f"before={before}")
    url = f"{base_url}/v2/history/get_actions?" + "&".join(params)
    req = urllib.request.Request(url, headers=HEADERS)

    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read()).get("actions", [])
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 30 * (attempt + 1)
                print(f"    [!] Rate limited — sleeping {wait}s")
                time.sleep(wait)
            else:
                print(f"    [!] HTTP {e.code} from {base_url}")
                time.sleep(5)
        except Exception as e:
            print(f"    [!] Fetch error (attempt {attempt+1}/{retries}): {e}")
            time.sleep(5)
    return []


# ─────────────────────────────────────────────────────────────────────────────
# USD reverse lookup — find prior n2 FIAT listing for a property
# ─────────────────────────────────────────────────────────────────────────────

def reverse_lookup_usd(base_url: str, property_id: str,
                        before_ts: str) -> tuple:
    url = (
        f"{base_url}/v2/history/get_actions"
        f"?account=playuplandme&filter=playuplandme:n2"
        f"&limit=20&sort=desc&before={before_ts}"
    )
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            actions = json.loads(r.read()).get("actions", [])
        for action in actions:
            d = action.get("act", {}).get("data", {})
            if str(d.get("a45", "")) == str(property_id):
                raw = d.get("p3", "")
                if raw and raw not in ("0.00 FIAT", "0 FIAT", ""):
                    try:
                        price = float(raw.replace(" FIAT", "").replace(",", ""))
                        if price > 0:
                            return price, d.get("a54")
                    except ValueError:
                        pass
    except Exception:
        pass
    return None, None


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

def process_actions(conn: sqlite3.Connection, actions: list, base_url: str) -> int:
    inserted = 0
    for action in actions:
        act  = action.get("act", {})
        name = act.get("name", "")
        data = act.get("data", {})
        trx_id    = action.get("trx_id", "")
        timestamp = action.get("@timestamp", "").rstrip("Z")
        block_num = action.get("block_num")

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
                conn.commit()

        # ── n4 : property unlisted ───────────────────────────────────────────
        elif name == "n4":
            prop_id = str(data.get("a45", ""))
            if prop_id:
                conn.execute(
                    "DELETE FROM pending_usd_listings WHERE property_id=?", (prop_id,)
                )
                conn.commit()

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
                        buyer,seller,upx_amount,marketplace,asset_type)
                       VALUES(?,?,?,?,?,?,?,?,?,NULL,?,'upx','property')""",
                    (trx_id, block_num, timestamp, "n5", prop_id,
                     meta["address"], meta["city"], meta["neighborhood"], buyer, upx),
                )
                if conn.execute("SELECT changes()").fetchone()[0]:
                    inserted += 1
                    _upsert_hourly(conn, timestamp, "upx", upx or 0)
                conn.commit()
            except Exception as e:
                print(f"    [!] n5 insert: {e}")

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
                conn.commit()
            else:
                usd_price, seller = reverse_lookup_usd(base_url, prop_id, timestamp)

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
                conn.commit()
            except Exception as e:
                print(f"    [!] n52 insert: {e}")

        # ── n111 / n112 : asset / NFT sales ─────────────────────────────────
        elif name in ("n111", "n112"):
            buyer  = data.get("p1") or data.get("p14")
            seller = data.get("p2") or data.get("p25")
            raw_price = data.get("p45") or data.get("p141")
            upx    = parse_upx(str(raw_price)) if raw_price else None
            atype  = "asset" if name == "n111" else "nft"
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
                conn.commit()
            except Exception as e:
                print(f"    [!] {name} insert: {e}")

    return inserted


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


# ─────────────────────────────────────────────────────────────────────────────
# Backfill — walk through both chains in order
# ─────────────────────────────────────────────────────────────────────────────

def backfill(conn: sqlite3.Connection) -> None:
    chains = [
        ("EOS",      EOS_URL,      EOS_START,      APPCHAIN_START),
        ("AppChain", APPCHAIN_URL, APPCHAIN_START, None),
    ]

    for chain_name, base_url, chain_start, chain_end in chains:
        state_key = f"backfill_cursor_{chain_name}"
        cursor    = get_state(conn, state_key, chain_start)
        done_key  = f"backfill_done_{chain_name}"

        if get_state(conn, done_key):
            print(f"[+] {chain_name} backfill already complete")
            continue

        print(f"\n[*] Backfilling {chain_name}  {cursor[:10]} → {chain_end[:10] if chain_end else 'present'}")
        total = 0
        batches = 0

        while True:
            actions = fetch_actions(base_url, after=cursor, before=chain_end, limit=BATCH_SIZE)

            if not actions:
                set_state(conn, done_key, "1")
                print(f"\n[+] {chain_name} backfill complete — {total:,} records inserted")
                break

            n       = process_actions(conn, actions, base_url)
            total  += n
            batches += 1
            cursor  = actions[-1].get("@timestamp", cursor)
            set_state(conn, state_key, cursor)

            if batches % 100 == 0:
                print(f"    {chain_name}  {cursor[:19]}  {total:,} inserted")

            time.sleep(BACKFILL_SLEEP)


# ─────────────────────────────────────────────────────────────────────────────
# Live polling
# ─────────────────────────────────────────────────────────────────────────────

def live_poll(conn: sqlite3.Connection) -> None:
    global _live_lookup_enabled
    _live_lookup_enabled = True
    now    = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000")
    cursor = get_state(conn, "live_cursor", now)
    print(f"\n[*] Live polling AppChain from {cursor[:19]}  (every {POLL_INTERVAL}s)")

    session_total = 0
    while True:
        actions = fetch_actions(APPCHAIN_URL, after=cursor, limit=BATCH_SIZE)
        if actions:
            n              = process_actions(conn, actions, APPCHAIN_URL)
            session_total += n
            cursor         = actions[-1].get("@timestamp", cursor)
            set_state(conn, "live_cursor", cursor)
            if n:
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                print(f"  [{ts}] +{n} new  (session total: {session_total:,})")
        time.sleep(POLL_INTERVAL)


# ─────────────────────────────────────────────────────────────────────────────
# Stats
# ─────────────────────────────────────────────────────────────────────────────

def print_stats(conn: sqlite3.Connection) -> None:
    total   = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    upx     = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(upx_amount),0) FROM transactions"
        " WHERE marketplace='upx' AND asset_type='property'"
    ).fetchone()
    usd     = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(usd_amount),0) FROM transactions"
        " WHERE marketplace='usd'"
    ).fetchone()
    assets  = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(upx_amount),0) FROM transactions"
        " WHERE asset_type IN ('asset','nft')"
    ).fetchone()
    oldest  = conn.execute("SELECT MIN(timestamp) FROM transactions").fetchone()[0]
    newest  = conn.execute("SELECT MAX(timestamp) FROM transactions").fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM pending_usd_listings").fetchone()[0]

    print(f"""
╔══════════════════════════════════════════════════╗
║  Economy DB — {DB_PATH.name}
╠══════════════════════════════════════════════════╣
║  Total records:      {total:>10,}
║  Date range:         {(oldest or '?')[:10]}  →  {(newest or '?')[:10]}
╠══════════════════════════════════════════════════╣
║  UPX property sales: {upx[0]:>10,}    {upx[1]:>16,.0f} UPX
║  USD property sales: {usd[0]:>10,}    ${usd[1]:>15,.2f}
║  Asset/NFT sales:    {assets[0]:>10,}    {assets[1]:>16,.0f} UPX
╠══════════════════════════════════════════════════╣
║  Pending USD listings: {pending:>8,}
╚══════════════════════════════════════════════════╝
""")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Upland Economy Scraper")
    parser.add_argument("--backfill-only", action="store_true",
                        help="Run backfill then exit (no live polling)")
    parser.add_argument("--live-only", action="store_true",
                        help="Skip backfill, poll live from now")
    parser.add_argument("--stats", action="store_true",
                        help="Print DB stats and exit")
    args = parser.parse_args()

    load_property_cache()
    conn = get_db()
    init_db(conn)

    if args.stats:
        print_stats(conn)
        return

    def _shutdown(sig, _frame):
        print("\n[!] Shutting down — flushing DB...")
        conn.commit()
        conn.close()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    print(f"[*] DB: {DB_PATH}")

    if not args.live_only:
        backfill(conn)

    if not args.backfill_only:
        live_poll(conn)


if __name__ == "__main__":
    main()
