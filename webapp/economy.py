"""
Economy dashboard DB queries — reads from economy.db written by the scraper.
All functions return plain dicts/lists; Flask routes handle JSON serialisation.
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone

DB_PATH = os.environ.get("ECONOMY_DB", os.path.join(os.path.dirname(__file__), "..", "data", "economy.db"))


def _connect():
    if not os.path.exists(DB_PATH):
        return None
    conn = sqlite3.connect(DB_PATH, timeout=3)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    return conn


def _period_start(period: str) -> str:
    now = datetime.now(timezone.utc)
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "7d":
        start = now - timedelta(days=7)
    elif period == "30d":
        start = now - timedelta(days=30)
    elif period == "90d":
        start = now - timedelta(days=90)
    else:
        start = datetime(2023, 3, 18, tzinfo=timezone.utc)
    return start.strftime("%Y-%m-%dT%H:%M:%S")


def summary(period: str = "30d") -> dict:
    conn = _connect()
    if not conn:
        return {"upx_volume": 0, "usd_volume": 0, "upx_trades": 0, "usd_trades": 0,
                "period": period, "period_start": None, "no_data": True}
    try:
        since = _period_start(period)
        rows = conn.execute("""
            SELECT marketplace,
                   COUNT(*) AS trade_count,
                   COALESCE(SUM(upx_amount), 0) AS upx_volume,
                   COALESCE(SUM(usd_amount), 0) AS usd_volume
            FROM transactions
            WHERE timestamp >= ?
            GROUP BY marketplace
        """, (since,)).fetchall()
        result = {"upx_volume": 0, "usd_volume": 0, "upx_trades": 0, "usd_trades": 0,
                  "period": period, "period_start": since, "no_data": False}
        for row in rows:
            if row["marketplace"] == "upx":
                result["upx_volume"] = row["upx_volume"]
                result["upx_trades"] = row["trade_count"]
            elif row["marketplace"] == "usd":
                result["usd_volume"] = row["usd_volume"]
                result["usd_trades"] = row["trade_count"]
        return result
    finally:
        conn.close()


def timeseries(period: str = "30d") -> list:
    conn = _connect()
    if not conn:
        return []
    try:
        since = _period_start(period)
        use_hourly = period in ("today", "7d")

        if use_hourly:
            # hourly_aggregates is already at hour granularity
            rows = conn.execute("""
                SELECT hour AS bucket, marketplace,
                       trade_count, volume
                FROM hourly_aggregates
                WHERE hour >= ?
                ORDER BY hour
            """, (since,)).fetchall()
        else:
            # roll up hourly_aggregates into daily buckets — much faster than scanning transactions
            rows = conn.execute("""
                SELECT strftime('%Y-%m-%d', hour) AS bucket,
                       marketplace,
                       SUM(trade_count) AS trade_count,
                       SUM(volume) AS volume
                FROM hourly_aggregates
                WHERE hour >= ?
                GROUP BY bucket, marketplace
                ORDER BY bucket
            """, (since,)).fetchall()

        buckets: dict = {}
        for row in rows:
            b = row["bucket"]
            if b not in buckets:
                buckets[b] = {"timestamp": b, "upx_volume": 0, "usd_volume": 0,
                               "upx_trades": 0, "usd_trades": 0}
            if row["marketplace"] == "upx":
                buckets[b]["upx_volume"] = row["volume"]
                buckets[b]["upx_trades"] = row["trade_count"]
            elif row["marketplace"] == "usd":
                buckets[b]["usd_volume"] = row["volume"]
                buckets[b]["usd_trades"] = row["trade_count"]
        return sorted(buckets.values(), key=lambda x: x["timestamp"])
    finally:
        conn.close()


def feed(limit: int = 50, marketplace: str = None, city: str = None) -> list:
    conn = _connect()
    if not conn:
        return []
    try:
        clauses, params = ["asset_type = 'property'"], []
        if marketplace in ("upx", "usd"):
            clauses.append("marketplace = ?"); params.append(marketplace)
        if city:
            clauses.append("city = ?"); params.append(city)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(f"""
            SELECT id, timestamp, address, city, neighborhood,
                   buyer, seller, upx_amount, usd_amount, marketplace, asset_type, action
            FROM transactions
            {where}
            ORDER BY id DESC LIMIT ?
        """, (*params, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def cities(period: str = "30d") -> list:
    conn = _connect()
    if not conn:
        return []
    try:
        since = _period_start(period)
        rows = conn.execute("""
            SELECT city,
                   COUNT(*) AS total_trades,
                   COUNT(CASE WHEN marketplace='upx' THEN 1 END) AS upx_trades,
                   COUNT(CASE WHEN marketplace='usd' THEN 1 END) AS usd_trades,
                   COALESCE(SUM(upx_amount), 0) AS upx_volume,
                   COALESCE(SUM(usd_amount), 0) AS usd_volume,
                   AVG(CASE WHEN marketplace='upx' AND upx_amount IS NOT NULL THEN upx_amount END) AS avg_upx,
                   AVG(CASE WHEN marketplace='usd' AND usd_amount IS NOT NULL THEN usd_amount END) AS avg_usd
            FROM transactions
            WHERE timestamp >= ? AND city IS NOT NULL AND city != ''
            GROUP BY city
            ORDER BY total_trades DESC
        """, (since,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def latest_since(last_id: int = 0, limit: int = 30, city: str = None) -> list:
    """Return transactions with id > last_id — used by feed polling."""
    conn = _connect()
    if not conn:
        return []
    try:
        if city:
            rows = conn.execute("""
                SELECT id, timestamp, address, city, neighborhood,
                       buyer, seller, upx_amount, usd_amount, marketplace, asset_type, action
                FROM transactions
                WHERE id > ? AND city = ? AND asset_type = 'property'
                ORDER BY id ASC LIMIT ?
            """, (last_id, city, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT id, timestamp, address, city, neighborhood,
                       buyer, seller, upx_amount, usd_amount, marketplace, asset_type, action
                FROM transactions
                WHERE id > ? AND asset_type = 'property'
                ORDER BY id ASC LIMIT ?
            """, (last_id, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def max_id() -> int:
    conn = _connect()
    if not conn:
        return 0
    try:
        row = conn.execute("SELECT MAX(id) AS m FROM transactions").fetchone()
        return row["m"] or 0
    finally:
        conn.close()
