"""SQLite snapshot of properties listed for sale right now.

Lives in data/listings.db, not economy.db. A bug in the crawler must not be
able to touch sale history. This module is network-free so the web app and
the tests can import it without API credentials.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "listings.db"
PROP_CACHE_DB = ROOT / "scraper" / "property_cache.db"

# Developers API: pageSize max 100 and currentPage max 99. A city scan past
# this cannot be complete, so it must not be treated as a full snapshot.
PAGE_SIZE = 100
PAGE_CAP = 99
FULL_CITY_MAX_PROPS = PAGE_SIZE * PAGE_CAP  # 9_900

_STREET_ABBREV = {
    "AVENUE": "AVE", "STREET": "ST", "BOULEVARD": "BLVD",
    "DRIVE": "DR", "ROAD": "RD", "LANE": "LN", "COURT": "CT",
    "PLACE": "PL", "CIRCLE": "CIR", "TERRACE": "TER",
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
}
_STRIP_SUFFIXES = {
    "ST", "AVE", "BLVD", "DR", "RD", "LN", "CT", "PL", "CIR",
    "TER", "WAY", "HWY", "FWY", "PKWY", "ESPL", "EXPY",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS current_listings (
    property_id    TEXT PRIMARY KEY,
    address        TEXT,
    city_id        INTEGER,
    city           TEXT,
    neighborhood   TEXT,
    currency       TEXT,
    price          REAL,
    mint_price     REAL,
    markup_pct     REAL,
    owner_username TEXT,
    last_seen      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_listings_markup ON current_listings(markup_pct);
CREATE INDEX IF NOT EXISTS idx_listings_city ON current_listings(city_id);
CREATE INDEX IF NOT EXISTS idx_listings_neighborhood ON current_listings(city_id, neighborhood);

CREATE TABLE IF NOT EXISTS crawl_targets (
    target_key     TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,
    city_id        INTEGER,
    city           TEXT,
    cache_city     TEXT,
    neighborhood   TEXT,
    total_props    INTEGER,
    units_done     INTEGER,
    units_total    INTEGER,
    status         TEXT NOT NULL,
    started_at     TEXT,
    finished_at    TEXT,
    listing_count  INTEGER,
    note           TEXT
);

CREATE TABLE IF NOT EXISTS crawl_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def fold(value: str | None) -> str:
    """Case- and accent-insensitive key for city-name matching."""
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text.casefold())
    return " ".join(text.split())


def markup_pct(currency: str | None, price, mint_price) -> float | None:
    """Percent over mint. USD and UPX are different markets, so only UPX gets one."""
    if currency != "UPX" or price is None or not mint_price:
        return None
    try:
        mint = float(mint_price)
        if mint <= 0:
            return None
        return round(100.0 * (float(price) - mint) / mint, 1)
    except (TypeError, ValueError):
        return None


def street_search_term(address: str | None) -> str | None:
    """Distinctive street tokens for the developers API textSearch.

    The API ORs every token, so a trailing 'ST' matches half the city.
    Terms shorter than 3 characters are rejected for the same reason.
    """
    text = (address or "").upper().strip()
    text = re.sub(r"^[0-9][0-9\-/]*\s+", "", text)
    if not text:
        return None
    tokens = [_STREET_ABBREV.get(tok, tok) for tok in text.split()]
    if tokens and tokens[-1] in _STRIP_SUFFIXES:
        tokens = tokens[:-1]
    term = " ".join(tokens).strip()
    if len(term) < 3:
        return None
    return term


def address_has_street(address: str | None, street: str | None) -> bool:
    """True when `street` appears as whole tokens in the address, house number aside."""
    term = street_search_term(street) or ""
    if not term:
        wanted = [tok for tok in (street or "").upper().split() if tok not in _STRIP_SUFFIXES]
        term = " ".join(wanted)
    if len(term) < 3:
        return False
    addr = (address or "").upper()
    addr = re.sub(r"^[0-9][0-9\-/]*\s+", "", addr)
    addr_tokens = [_STREET_ABBREV.get(tok, tok) for tok in addr.split()]
    term_tokens = term.split()
    if not term_tokens or len(term_tokens) > len(addr_tokens):
        return False
    width = len(term_tokens)
    return any(addr_tokens[i:i + width] == term_tokens for i in range(len(addr_tokens) - width + 1))


def connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = Path(path) if path else DEFAULT_DB
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    return conn


def get_state(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM crawl_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO crawl_state(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def match_city(cache_name: str, api_cities: list[dict]) -> dict | None:
    """Match a property-cache city name to a developers-API city. Exact folded name only."""
    key = fold(cache_name)
    if not key:
        return None
    hits = [city for city in api_cities if fold(city.get("name")) == key]
    if len(hits) == 1:
        return hits[0]
    return None


def plan_targets(
    owned: list[dict],
    city_sizes: dict[int, int | None],
    *,
    max_city_props: int = FULL_CITY_MAX_PROPS,
    include_large: bool = False,
) -> list[dict]:
    """Decide what to crawl.

    A full-city page scan is only honest when the city fits in the API's
    99-page window. Larger cities the user owns are deferred, and each
    neighborhood they own there is crawled by street search instead.
    `include_large` still cannot see past the page cap: those city targets
    are flagged expect_partial so the crawler will not delete unseen rows.
    """
    by_city: dict[int, dict] = {}
    for row in owned:
        city_id = row.get("city_id")
        if city_id is None:
            continue
        bucket = by_city.setdefault(int(city_id), {
            "city_id": int(city_id),
            "city": row.get("city") or "",
            "cache_city": row.get("cache_city") or row.get("city") or "",
            "neighborhoods": set(),
        })
        hood = (row.get("neighborhood") or "").strip()
        if hood:
            bucket["neighborhoods"].add(hood.upper())

    targets: list[dict] = []
    for city in by_city.values():
        total = city_sizes.get(city["city_id"])
        base = {
            "city_id": city["city_id"],
            "city": city["city"],
            "cache_city": city["cache_city"],
            "total_props": total,
        }
        fits = total is not None and total <= max_city_props and total <= FULL_CITY_MAX_PROPS
        unknown = total is None
        if fits or unknown:
            targets.append({
                **base,
                "key": f"city:{city['city_id']}",
                "kind": "city",
                "neighborhood": None,
                "expect_partial": False,
                "note": None if not unknown else "city size unknown; scan until the API runs out",
            })
            continue
        if include_large:
            targets.append({
                **base,
                "key": f"city:{city['city_id']}",
                "kind": "city",
                "neighborhood": None,
                "expect_partial": True,
                "note": (
                    f"{total:,} properties exceeds the {FULL_CITY_MAX_PROPS:,}-property "
                    "API page cap; snapshot will be partial and will not drop unseen listings"
                ),
            })
            continue
        targets.append({
            **base,
            "key": f"city:{city['city_id']}",
            "kind": "deferred",
            "neighborhood": None,
            "expect_partial": False,
            "note": (
                f"{total:,} properties exceeds {max_city_props:,}; "
                "crawling owned neighborhoods by street instead"
            ),
        })
        for hood in sorted(city["neighborhoods"]):
            targets.append({
                **base,
                "key": f"hood:{city['city_id']}:{hood}",
                "kind": "neighborhood",
                "neighborhood": hood,
                "expect_partial": False,
                "note": None,
            })

    def sort_key(target: dict):
        kind_order = {"city": 0, "neighborhood": 1, "deferred": 2}[target["kind"]]
        total = target["total_props"] if target["total_props"] is not None else 10 ** 12
        return (kind_order, total, target["city"], target.get("neighborhood") or "")

    return sorted(targets, key=sort_key)


def upsert_listing(conn: sqlite3.Connection, rec: dict) -> None:
    """Insert or refresh one listing.

    A failed price lookup must not wipe a price we already stored. Passing
    price=None keeps the previous price, currency, and markup, and still
    moves last_seen forward so a later sweep does not treat the row as gone.
    """
    prop_id = str(rec["property_id"])
    prev = conn.execute(
        "SELECT price, currency, markup_pct, owner_username, mint_price "
        "FROM current_listings WHERE property_id = ?",
        (prop_id,),
    ).fetchone()

    price = rec.get("price")
    currency = rec.get("currency") or None
    mint = rec.get("mint_price")
    owner = rec.get("owner_username")
    if price is None and prev is not None:
        price = prev["price"]
        currency = prev["currency"]
        mint = mint if mint is not None else prev["mint_price"]
        owner = owner or prev["owner_username"]
        markup = prev["markup_pct"] if rec.get("mint_price") is None else markup_pct(currency, price, mint)
    else:
        markup = markup_pct(currency, price, mint)

    neighborhood = (rec.get("neighborhood") or "").upper() or None
    conn.execute(
        """
        INSERT INTO current_listings (
            property_id, address, city_id, city, neighborhood,
            currency, price, mint_price, markup_pct, owner_username, last_seen
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(property_id) DO UPDATE SET
            address = excluded.address,
            city_id = excluded.city_id,
            city = excluded.city,
            neighborhood = excluded.neighborhood,
            currency = excluded.currency,
            price = excluded.price,
            mint_price = excluded.mint_price,
            markup_pct = excluded.markup_pct,
            owner_username = excluded.owner_username,
            last_seen = excluded.last_seen
        """,
        (
            prop_id,
            rec.get("address") or "",
            rec.get("city_id"),
            rec.get("city") or "",
            neighborhood,
            currency,
            price,
            mint,
            markup,
            owner,
            rec.get("last_seen") or utc_now(),
        ),
    )


def delete_unseen(
    conn: sqlite3.Connection,
    *,
    city_id: int,
    neighborhood: str | None,
    started_at: str,
) -> int:
    """Drop listings in this scope that this crawl did not confirm.

    Neighborhood scope must not touch the rest of the city. Callers may only
    use this after a scan that actually covered the scope.
    """
    if neighborhood:
        cur = conn.execute(
            "DELETE FROM current_listings WHERE city_id = ? AND neighborhood = ? AND last_seen < ?",
            (city_id, neighborhood.upper(), started_at),
        )
    else:
        cur = conn.execute(
            "DELETE FROM current_listings WHERE city_id = ? AND last_seen < ?",
            (city_id, started_at),
        )
    return cur.rowcount


def save_target(conn: sqlite3.Connection, target: dict) -> None:
    conn.execute(
        """
        INSERT INTO crawl_targets (
            target_key, kind, city_id, city, cache_city, neighborhood,
            total_props, units_done, units_total, status, started_at,
            finished_at, listing_count, note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(target_key) DO UPDATE SET
            kind = excluded.kind,
            city_id = excluded.city_id,
            city = excluded.city,
            cache_city = excluded.cache_city,
            neighborhood = excluded.neighborhood,
            total_props = excluded.total_props,
            units_done = excluded.units_done,
            units_total = excluded.units_total,
            status = excluded.status,
            started_at = excluded.started_at,
            finished_at = excluded.finished_at,
            listing_count = excluded.listing_count,
            note = excluded.note
        """,
        (
            target["key"],
            target["kind"],
            target.get("city_id"),
            target.get("city"),
            target.get("cache_city"),
            (target.get("neighborhood") or None),
            target.get("total_props"),
            target.get("units_done"),
            target.get("units_total"),
            target.get("status") or "pending",
            target.get("started_at"),
            target.get("finished_at"),
            target.get("listing_count"),
            target.get("note"),
        ),
    )


def get_target(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM crawl_targets WHERE target_key = ?", (key,)).fetchone()


def count_listings(conn: sqlite3.Connection, *, city_id: int, neighborhood: str | None = None) -> int:
    if neighborhood:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM current_listings WHERE city_id = ? AND neighborhood = ?",
            (city_id, neighborhood.upper()),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM current_listings WHERE city_id = ?",
            (city_id,),
        ).fetchone()
    return int(row["n"])


def target_is_fresh(conn: sqlite3.Connection, key: str, max_age_hours: float) -> bool:
    row = get_target(conn, key)
    if row is None or row["status"] != "complete" or not row["finished_at"]:
        return False
    finished = parse_time(row["finished_at"])
    if finished is None:
        return False
    age = datetime.now(timezone.utc) - finished
    return age.total_seconds() <= max_age_hours * 3600


def _listing_view(row: sqlite3.Row) -> dict:
    currency = row["currency"] or ""
    price = row["price"]
    return {
        "id": row["property_id"],
        "address": row["address"] or "",
        "city": row["city"] or "",
        "city_id": row["city_id"],
        "neighborhood": row["neighborhood"] or "",
        "currency": currency,
        "price": price,
        "price_upx": price if currency == "UPX" else None,
        "price_usd": price if currency == "USD" else None,
        "mint_price": row["mint_price"],
        "markup_pct": row["markup_pct"],
        "owner_username": row["owner_username"] or "",
        "last_seen": row["last_seen"],
    }


def listings_for_requirement(
    conn: sqlite3.Connection,
    *,
    city_id: int,
    neighborhood: str | None = None,
    street: str | None = None,
    max_age_hours: float = 36,
    limit: int = 200,
    require_fresh: bool = True,
) -> list[dict] | None:
    """Listings that satisfy a collection requirement, or None if the snapshot can't answer.

    None means the caller should fall back to a live scan. With require_fresh
    (the default), an empty list means the snapshot is fresh and nothing is
    listed. Street requirements need a complete city scan. A single neighborhood
    does not cover every property on a street.

    require_fresh=False still applies those filters, but an empty list only
    means none of the stored rows matched. It does not mean the city was crawled,
    and it must not be stored as finished coverage.
    """
    city_id = int(city_id)
    hood = (neighborhood or "").strip().upper() or None
    if street:
        if require_fresh and not target_is_fresh(conn, f"city:{city_id}", max_age_hours):
            return None
    elif hood:
        fresh = target_is_fresh(conn, f"city:{city_id}", max_age_hours) or target_is_fresh(
            conn, f"hood:{city_id}:{hood}", max_age_hours
        )
        if require_fresh and not fresh:
            return None
    else:
        return None

    clauses = ["city_id = ?"]
    params: list = [city_id]
    if hood and not street:
        clauses.append("neighborhood = ?")
        params.append(hood)
    if street:
        term = street_search_term(street) or street.upper().strip()
        if len(term) < 3:
            return None
        clauses.append("INSTR(UPPER(address), ?) > 0")
        params.append(term)
    rows = conn.execute(
        f"SELECT * FROM current_listings WHERE {' AND '.join(clauses)}",
        params,
    ).fetchall()
    if street:
        rows = [row for row in rows if address_has_street(row["address"], street)]
    views = [_listing_view(row) for row in rows]
    views.sort(key=lambda item: (
        item["markup_pct"] is None,
        item["markup_pct"] if item["markup_pct"] is not None else 10 ** 9,
        item["price_upx"] is None,
        item["price_upx"] or 10 ** 12,
    ))
    return views[:limit]


def partial_snapshot_note(conn: sqlite3.Connection) -> str:
    """Say that stored rows are not a finished city crawl."""
    count = int(summary(conn)["listings"] or 0)
    noun = "listing" if count == 1 else "listings"
    return (
        f"Showing matches from the local snapshot ({count} {noun}). "
        "That snapshot is not a full city scan, so a missing property may still be for sale."
    )


def search_listings(
    conn: sqlite3.Connection,
    *,
    currency: str | None = None,
    city: str | None = None,
    neighborhood: str | None = None,
    sort: str = "markup",
    limit: int = 100,
) -> list[dict]:
    clauses = []
    params: list = []
    if currency:
        clauses.append("currency = ?")
        params.append(currency)
    if city:
        clauses.append("LOWER(city) = LOWER(?)")
        params.append(city)
    if neighborhood:
        clauses.append("LOWER(neighborhood) = LOWER(?)")
        params.append(neighborhood)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    if sort == "price":
        order = "price IS NULL, price ASC, address"
    elif sort == "seen":
        order = "last_seen DESC, address"
    else:
        order = "markup_pct IS NULL, markup_pct ASC, price IS NULL, price ASC"
    params.append(int(limit))
    rows = conn.execute(
        f"SELECT * FROM current_listings {where} ORDER BY {order} LIMIT ?",
        params,
    ).fetchall()
    return [_listing_view(row) for row in rows]


def distinct_values(conn: sqlite3.Connection, column: str) -> list[str]:
    if column not in {"city", "neighborhood", "currency"}:
        raise ValueError(column)
    rows = conn.execute(
        f"SELECT DISTINCT {column} AS v FROM current_listings "
        f"WHERE {column} IS NOT NULL AND {column} != '' ORDER BY v"
    ).fetchall()
    return [row["v"] for row in rows]


def coverage_rows(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM crawl_targets ORDER BY "
        "CASE status WHEN 'running' THEN 0 WHEN 'partial' THEN 1 WHEN 'error' THEN 2 "
        "WHEN 'complete' THEN 3 WHEN 'pending' THEN 4 ELSE 5 END, city, neighborhood"
    ).fetchall()
    return [dict(row) for row in rows]


def summary(conn: sqlite3.Connection) -> dict:
    counts = conn.execute(
        "SELECT "
        "COUNT(*) AS listings, "
        "SUM(CASE WHEN currency = 'UPX' THEN 1 ELSE 0 END) AS upx, "
        "SUM(CASE WHEN currency = 'USD' THEN 1 ELSE 0 END) AS usd, "
        "MAX(last_seen) AS freshest_listing "
        "FROM current_listings"
    ).fetchone()
    targets = conn.execute(
        "SELECT status, COUNT(*) AS n FROM crawl_targets GROUP BY status"
    ).fetchall()
    by_status = {row["status"]: row["n"] for row in targets}
    freshest_crawl = conn.execute(
        "SELECT MAX(finished_at) AS t FROM crawl_targets WHERE status = 'complete'"
    ).fetchone()
    return {
        "listings": counts["listings"] or 0,
        "upx": counts["upx"] or 0,
        "usd": counts["usd"] or 0,
        "freshest_listing": counts["freshest_listing"],
        "freshest_crawl": freshest_crawl["t"],
        "targets": by_status,
    }
