"""
Economy dashboard DB queries — reads from economy.db written by the scraper.
All functions return plain dicts/lists; Flask routes handle JSON serialisation.
"""

import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = os.environ.get("ECONOMY_DB", os.path.join(os.path.dirname(__file__), "..", "data", "economy.db"))
PROP_CACHE_DB = os.path.join(os.path.dirname(__file__), "..", "scraper", "property_cache.db")

_AGG_CACHE_DIR = Path(os.environ.get("CACHE_DIR", os.path.join(os.path.dirname(__file__), "cache"))) / "economy_agg"
_AGG_CACHE_TTL = 900  # 15 min — these are full-table scans for longer periods (up to ~11s for "all")


def _connect():
    if not os.path.exists(DB_PATH):
        return None
    conn = sqlite3.connect(DB_PATH, timeout=3)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    return conn


def _connect_with_property_cache():
    """
    Like _connect(), but also ATTACHes scraper/property_cache.db so queries can
    join on property_id for reliable city/neighborhood — most transactions rows
    don't have those columns populated directly except for recent data (~100%
    for the last 30d, dropping to ~15% for all-time history), since the scraper
    only inlines them when it already had the property cached at insert time.
    """
    conn = _connect()
    if conn is None:
        return None
    if os.path.exists(PROP_CACHE_DB):
        conn.execute(f"ATTACH DATABASE '{PROP_CACHE_DB}' AS pc")
    return conn


def _ttl(period: str) -> int:
    # Today changes quickly. Longer periods are the scans that stall the Pi.
    return 60 if period == "today" else _AGG_CACHE_TTL


def _cache_path(key: str) -> Path:
    # The file name includes the database path so a test database cannot
    # reuse a cache written for the live database.
    tag = hashlib.sha1(os.path.abspath(DB_PATH).encode()).hexdigest()[:12]
    return _AGG_CACHE_DIR / f"{key}_{tag}.json"


def _agg_cache_get(key: str, ttl: int = _AGG_CACHE_TTL):
    path = _cache_path(key)
    if path.exists() and (time.time() - path.stat().st_mtime) < ttl:
        try:
            return json.loads(path.read_text())
        except Exception:
            return None
    return None


def _agg_cache_set(key: str, value) -> None:
    _AGG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(key).write_text(json.dumps(value))


def _previous_start(period: str) -> str | None:
    """Start of the window before this period. All-time has no pair."""
    now = datetime.now(timezone.utc)
    if period == "today":
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "7d":
        start = now - timedelta(days=14)
    elif period == "30d":
        start = now - timedelta(days=60)
    elif period == "90d":
        start = now - timedelta(days=180)
    else:
        return None
    return start.strftime("%Y-%m-%dT%H:%M:%S")


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


def _median_property_upx(conn, since: str):
    """Median UPX price of property sales since `since`. Ignores spark and NFT sales."""
    amounts = [
        row[0] for row in conn.execute("""
            SELECT upx_amount FROM transactions
            WHERE timestamp >= ?
              AND marketplace = 'upx'
              AND asset_type = 'property'
              AND upx_amount > 0
            ORDER BY upx_amount
        """, (since,))
    ]
    count = len(amounts)
    if not count:
        return None
    mid = count // 2
    if count % 2:
        return amounts[mid]
    return (amounts[mid - 1] + amounts[mid]) / 2


def _blank_summary(period: str, since: str = None, no_data: bool = True) -> dict:
    return {
        "upx_volume": 0, "usd_volume": 0, "upx_trades": 0, "usd_trades": 0,
        "period": period, "period_start": since, "no_data": no_data,
        "median_upx": None, "latest_sale": None,
        "property_upx_trades": 0, "property_upx_volume": 0,
        "asset_upx_trades": 0, "asset_upx_volume": 0,
        "nft_upx_trades": 0, "nft_upx_volume": 0,
        "previous_property_upx_volume": 0,
        "property_upx_change_pct": None,
    }


def summary(period: str = "30d") -> dict:
    cache_key = f"summary_{period}"
    cached = _agg_cache_get(cache_key, _ttl(period))
    if cached is not None:
        return cached
    conn = _connect()
    if not conn:
        return _blank_summary(period)
    try:
        since = _period_start(period)
        previous_since = _previous_start(period)
        # One timestamp scan covers this period and the one before it.
        # All-time stays a single scan. Grouping by asset_type splits
        # property sales from spark/equipment and NFTs in that same pass.
        if previous_since:
            rows = conn.execute("""
                SELECT CASE WHEN timestamp >= ? THEN 'current' ELSE 'previous' END AS span,
                       marketplace, asset_type,
                       COUNT(*) AS trade_count,
                       COALESCE(SUM(upx_amount), 0) AS upx_volume,
                       COALESCE(SUM(usd_amount), 0) AS usd_volume
                FROM transactions
                WHERE timestamp >= ?
                GROUP BY span, marketplace, asset_type
            """, (since, previous_since)).fetchall()
        else:
            rows = conn.execute("""
                SELECT 'current' AS span, marketplace, asset_type,
                       COUNT(*) AS trade_count,
                       COALESCE(SUM(upx_amount), 0) AS upx_volume,
                       COALESCE(SUM(usd_amount), 0) AS usd_volume
                FROM transactions
                WHERE timestamp >= ?
                GROUP BY marketplace, asset_type
            """, (since,)).fetchall()
        result = _blank_summary(period, since, no_data=False)
        for row in rows:
            if row["span"] == "previous":
                if row["marketplace"] == "upx" and row["asset_type"] == "property":
                    result["previous_property_upx_volume"] += row["upx_volume"]
                continue
            if row["marketplace"] == "upx":
                result["upx_volume"] += row["upx_volume"]
                result["upx_trades"] += row["trade_count"]
                kind = row["asset_type"] if row["asset_type"] in ("property", "asset", "nft") else None
                if kind:
                    result[f"{kind}_upx_trades"] += row["trade_count"]
                    result[f"{kind}_upx_volume"] += row["upx_volume"]
            elif row["marketplace"] == "usd":
                result["usd_volume"] += row["usd_volume"]
                result["usd_trades"] += row["trade_count"]
        previous_property = result["previous_property_upx_volume"]
        if previous_property:
            result["property_upx_change_pct"] = round(
                100 * (result["property_upx_volume"] - previous_property) / previous_property, 1
            )
        latest = conn.execute("SELECT MAX(timestamp) AS latest FROM transactions").fetchone()
        result["latest_sale"] = latest["latest"] if latest else None
        # All-time is a full-table sort on the Pi database. Skip the median there.
        if period != "all":
            result["median_upx"] = _median_property_upx(conn, since)
        if not result.get("no_data"):
            _agg_cache_set(cache_key, result)
        return result
    finally:
        conn.close()


def _empty_bucket(timestamp: str) -> dict:
    return {
        "timestamp": timestamp,
        "upx_volume": 0,
        "usd_volume": 0,
        "upx_trades": 0,
        "usd_trades": 0,
        "property_upx_volume": 0,
        "other_upx_volume": 0,
    }


def _add_upx_split(conn, since: str, buckets: dict, hourly: bool) -> None:
    """Add property vs spark/NFT UPX volume for one period. Uses the timestamp index."""
    bucket_expr = "substr(timestamp, 1, 13) || ':00:00'" if hourly else "substr(timestamp, 1, 10)"
    for bucket in buckets.values():
        bucket["property_upx_volume"] = 0
        bucket["other_upx_volume"] = 0
    rows = conn.execute(f"""
        SELECT {bucket_expr} AS bucket,
               CASE WHEN asset_type = 'property' THEN 'property' ELSE 'other' END AS kind,
               COALESCE(SUM(upx_amount), 0) AS volume
        FROM transactions
        WHERE timestamp >= ?
          AND marketplace = 'upx'
          AND upx_amount > 0
        GROUP BY bucket, kind
    """, (since,)).fetchall()
    for row in rows:
        bucket = buckets.get(row["bucket"])
        if bucket is None:
            bucket = _empty_bucket(row["bucket"])
            buckets[row["bucket"]] = bucket
        key = "property_upx_volume" if row["kind"] == "property" else "other_upx_volume"
        bucket[key] = row["volume"]


def timeseries(period: str = "30d") -> list:
    cache_key = f"timeseries_{period}"
    cached = _agg_cache_get(cache_key, _ttl(period))
    if cached is not None:
        return cached
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
        # All-time stays on the stored hourly totals. Splitting that period
        # would group the whole sales table. Shorter periods separate property
        # UPX from spark and NFT sales.
        if period != "all":
            _add_upx_split(conn, since, buckets, hourly=use_hourly)
        points = sorted(buckets.values(), key=lambda x: x["timestamp"])
        _agg_cache_set(cache_key, points)
        return points
    finally:
        conn.close()


# Stored sale kinds. n111 is one type for spark and equipment; the chain row
# does not say which. n112 is an NFT. Default the feed to properties so spark
# sales do not bury the property feed.
FEED_ASSETS = ("property", "asset", "nft")

_FEED_COLUMNS = """
    id, timestamp, address, city, neighborhood,
    buyer, seller, upx_amount, usd_amount, marketplace, asset_type, action,
    property_id
"""


def normalize_asset(asset: str = None) -> str:
    if asset in FEED_ASSETS or asset == "all":
        return asset
    return "property"


def _feed_where(asset: str, marketplace: str = None, city: str = None, after_id: int = None):
    clauses, params = [], []
    if after_id is not None:
        clauses.append("id > ?")
        params.append(after_id)
    if asset in FEED_ASSETS:
        clauses.append("asset_type = ?")
        params.append(asset)
    if marketplace in ("upx", "usd"):
        clauses.append("marketplace = ?")
        params.append(marketplace)
    if city:
        clauses.append("city = ?")
        params.append(city)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def feed(limit: int = 50, marketplace: str = None, city: str = None, asset: str = None) -> list:
    conn = _connect()
    if not conn:
        return []
    try:
        where, params = _feed_where(normalize_asset(asset), marketplace, city)
        rows = conn.execute(f"""
            SELECT {_FEED_COLUMNS}
            FROM transactions
            {where}
            ORDER BY id DESC LIMIT ?
        """, (*params, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def cities(period: str = "30d") -> list:
    cache_key = f"cities_{period}"
    cached = _agg_cache_get(cache_key, _ttl(period))
    if cached is not None:
        return cached

    conn = _connect_with_property_cache()
    if not conn:
        return []
    try:
        since = _period_start(period)
        rows = conn.execute("""
            SELECT COALESCE(pc.city, t.city) AS city,
                   COUNT(*) AS total_trades,
                   COUNT(CASE WHEN t.marketplace='upx' THEN 1 END) AS upx_trades,
                   COUNT(CASE WHEN t.marketplace='usd' THEN 1 END) AS usd_trades,
                   COALESCE(SUM(t.upx_amount), 0) AS upx_volume,
                   COALESCE(SUM(t.usd_amount), 0) AS usd_volume,
                   AVG(CASE WHEN t.marketplace='upx' AND t.upx_amount IS NOT NULL THEN t.upx_amount END) AS avg_upx,
                   AVG(CASE WHEN t.marketplace='usd' AND t.usd_amount IS NOT NULL THEN t.usd_amount END) AS avg_usd
            FROM transactions t
            LEFT JOIN pc.properties pc ON t.property_id = pc.prop_id
            WHERE t.timestamp >= ? AND t.asset_type = 'property'
              AND COALESCE(pc.city, t.city) IS NOT NULL AND COALESCE(pc.city, t.city) != ''
            GROUP BY COALESCE(pc.city, t.city)
            ORDER BY total_trades DESC
        """, (since,)).fetchall()
        result = [dict(r) for r in rows]
        _agg_cache_set(cache_key, result)
        return result
    finally:
        conn.close()


def neighborhoods(period: str = "30d", min_trades: int = 3) -> list:
    """
    Per-neighborhood price breakdown — which neighborhoods are hot vs
    depressed, by trade volume and average sale price. Same reliable
    property_cache.db join as cities() (transactions.neighborhood is even
    sparser than transactions.city historically). min_trades filters out
    neighborhoods with too few sales for the average to mean anything.
    """
    cache_key = f"neighborhoods_{period}_{min_trades}"
    cached = _agg_cache_get(cache_key, _ttl(period))
    if cached is not None:
        return cached

    conn = _connect_with_property_cache()
    if not conn:
        return []
    try:
        since = _period_start(period)
        rows = conn.execute("""
            SELECT COALESCE(pc.neighborhood, t.neighborhood) AS neighborhood,
                   COALESCE(pc.city, t.city) AS city,
                   COUNT(*) AS total_trades,
                   COUNT(CASE WHEN t.marketplace='upx' THEN 1 END) AS upx_trades,
                   COUNT(CASE WHEN t.marketplace='usd' THEN 1 END) AS usd_trades,
                   COALESCE(SUM(t.upx_amount), 0) AS upx_volume,
                   COALESCE(SUM(t.usd_amount), 0) AS usd_volume,
                   AVG(CASE WHEN t.marketplace='upx' AND t.upx_amount IS NOT NULL THEN t.upx_amount END) AS avg_upx,
                   AVG(CASE WHEN t.marketplace='usd' AND t.usd_amount IS NOT NULL THEN t.usd_amount END) AS avg_usd
            FROM transactions t
            LEFT JOIN pc.properties pc ON t.property_id = pc.prop_id
            WHERE t.timestamp >= ? AND t.asset_type = 'property'
              AND COALESCE(pc.neighborhood, t.neighborhood) IS NOT NULL
              AND COALESCE(pc.neighborhood, t.neighborhood) != ''
            GROUP BY COALESCE(pc.neighborhood, t.neighborhood), COALESCE(pc.city, t.city)
            HAVING COUNT(*) >= ?
            ORDER BY total_trades DESC
        """, (since, min_trades)).fetchall()
        result = [dict(r) for r in rows]
        _agg_cache_set(cache_key, result)
        return result
    finally:
        conn.close()


# One sales read. Callers that pass more names only get the first 400.
_MAX_SALE_MEDIAN_NEIGHBORHOODS = 400

# Shared by /cities and /markets. An ask more than twice the sale median is over 100%.
SALE_NEAR_PCT = 25
SALE_DOUBLE_PCT = 100


def sale_distance_counts(percents: list) -> dict:
    """Count asks in exclusive bands. Each percent is already rounded to 0.1.

    at or under, within 25% over, 25-100% over, and more than double.
    within_near still counts both sides of the median, so it overlaps the
    first two bands.
    """
    at_or_under = near_over = up_to_double = over_double = within_near = 0
    for pct in percents:
        if abs(pct) <= SALE_NEAR_PCT:
            within_near += 1
        if pct <= 0:
            at_or_under += 1
        elif pct <= SALE_NEAR_PCT:
            near_over += 1
        elif pct <= SALE_DOUBLE_PCT:
            up_to_double += 1
        else:
            over_double += 1
    return {
        "at_or_under": at_or_under,
        "near_over": near_over,
        "up_to_double": up_to_double,
        "over_double": over_double,
        "within_near": within_near,
    }


def neighborhood_sale_medians(neighborhoods: list, days: int = 90, minimum_sales: int = 5) -> dict:
    """Median UPX property sale for each neighborhood over the last `days`.

    Uses stored sales only. A neighborhood with fewer than `minimum_sales`
    is left out. Names match without regard to capitalization. At most 400
    names are read; later names are left out. Returns
    {requested_name: {"median_upx": n, "sales": n}}.
    """
    wanted = {}
    for name in neighborhoods:
        text = (name or "").strip()
        if text and text.casefold() not in wanted:
            wanted[text.casefold()] = text
        if len(wanted) >= _MAX_SALE_MEDIAN_NEIGHBORHOODS:
            break
    if not wanted or not os.path.exists(DB_PATH) or os.path.getsize(DB_PATH) < 100:
        return {}
    names = list(wanted.values())
    folds = [name.casefold() for name in names]
    cache_key = "sale_median_{0}_{1}_{2}".format(
        days, minimum_sales, hashlib.sha1("\n".join(sorted(folds)).encode()).hexdigest()[:16]
    )
    cached = _agg_cache_get(cache_key, _AGG_CACHE_TTL)
    if cached is not None:
        return cached

    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    conn = _connect()
    if conn is None:
        return {}
    placeholders = ",".join("?" * len(folds))
    try:
        rows = conn.execute(
            f"""
            SELECT neighborhood, upx_amount
            FROM transactions
            WHERE timestamp >= ?
              AND asset_type = 'property'
              AND marketplace = 'upx'
              AND upx_amount > 0
              AND neighborhood IS NOT NULL
              AND lower(neighborhood) IN ({placeholders})
            """,
            (since, *folds),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()

    grouped: dict = {}
    for row in rows:
        grouped.setdefault((row["neighborhood"] or "").casefold(), []).append(row["upx_amount"])
    result = {}
    for fold, requested in wanted.items():
        amounts = grouped.get(fold) or []
        if len(amounts) < minimum_sales:
            continue
        amounts.sort()
        mid = len(amounts) // 2
        if len(amounts) % 2:
            median = amounts[mid]
        else:
            median = (amounts[mid - 1] + amounts[mid]) / 2
        if not median:
            continue
        result[requested] = {"median_upx": median, "sales": len(amounts)}
    _agg_cache_set(cache_key, result)
    return result


def whales(period: str = "30d", limit: int = 20) -> dict:
    cache_key = f"whales_{period}_{limit}"
    cached = _agg_cache_get(cache_key, _ttl(period))
    if cached is not None:
        return cached
    conn = _connect()
    if not conn:
        return {"buyers": [], "sellers": []}
    try:
        since = _period_start(period)
        buyers = conn.execute("""
            SELECT buyer AS account,
                   COUNT(*) AS trades,
                   COALESCE(SUM(upx_amount), 0) AS upx_spent,
                   COALESCE(SUM(usd_amount), 0) AS usd_spent
            FROM transactions
            WHERE timestamp >= ? AND asset_type='property' AND buyer IS NOT NULL
            GROUP BY buyer
            ORDER BY upx_spent + usd_spent * 1000 DESC
            LIMIT ?
        """, (since, limit)).fetchall()
        sellers = conn.execute("""
            SELECT seller AS account,
                   COUNT(*) AS trades,
                   COALESCE(SUM(upx_amount), 0) AS upx_earned,
                   COALESCE(SUM(usd_amount), 0) AS usd_earned
            FROM transactions
            WHERE timestamp >= ? AND asset_type='property' AND seller IS NOT NULL
            GROUP BY seller
            ORDER BY upx_earned + usd_earned * 1000 DESC
            LIMIT ?
        """, (since, limit)).fetchall()
        result = {"buyers": [dict(r) for r in buyers], "sellers": [dict(r) for r in sellers]}
        _agg_cache_set(cache_key, result)
        return result
    finally:
        conn.close()


def latest_since(last_id: int = 0, limit: int = 30, city: str = None, asset: str = None) -> list:
    """Return transactions with id > last_id — used by feed polling."""
    conn = _connect()
    if not conn:
        return []
    try:
        where, params = _feed_where(normalize_asset(asset), city=city, after_id=last_id)
        rows = conn.execute(f"""
            SELECT {_FEED_COLUMNS}
            FROM transactions
            {where}
            ORDER BY id ASC LIMIT ?
        """, (*params, limit)).fetchall()
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
