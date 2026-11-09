#!/usr/bin/env python3
"""
For-sale listing snapshot.

Keeps data/listings.db current enough to answer "what is listed, and at what
markup over mint" with a SQL query instead of a live city crawl.

The developers API has no for-sale filter and will not page past 9,900
properties (pageSize 100, currentPage max 99). Cities that fit in that window
are scanned page by page. Larger cities the account owns are not scanned
whole: each owned neighborhood is searched by street name, which is the same
trick neighborhood_map uses. A scan that did not cover its whole scope is
saved as partial and does not delete listings it failed to revisit.

Usage:
  python3 listings_snapshot.py                  # resume the owned-city crawl
  python3 listings_snapshot.py --rebuild        # rebuild the queue, then crawl
  python3 listings_snapshot.py --dry-queue      # print the plan and exit
  python3 listings_snapshot.py --city "Park City"
  python3 listings_snapshot.py --city "Staten Island" --neighborhood "Dongan Hills"
  python3 listings_snapshot.py --stats
  python3 listings_snapshot.py --query --limit 20
  python3 listings_snapshot.py --reprice-caches
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import signal
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from listings_db import (
    DEFAULT_DB,
    FULL_CITY_MAX_PROPS,
    PAGE_CAP,
    PAGE_SIZE,
    PROP_CACHE_DB,
    ROOT,
    connect,
    count_listings,
    coverage_rows,
    delete_unseen,
    fold,
    get_state,
    match_city,
    plan_targets,
    save_target,
    search_listings,
    set_state,
    street_search_term,
    summary,
    target_is_fresh,
    upsert_listing,
    utc_now,
)

DEV_API = "https://api.prod.upland.me/developers-api"
PUBLIC_PROP = "https://api.upland.me/properties/{prop_id}"
BLOCKCHAIN_CACHE = ROOT / "webapp" / "cache" / "neighborhoods"

_stop = False


class CrawlStopped(Exception):
    pass


class ApiError(RuntimeError):
    pass


def _load_env() -> None:
    for path in (ROOT / ".env", ROOT / "scraper" / ".env"):
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        return


def _auth_headers() -> dict:
    app_id = os.environ.get("UPLAND_APP_ID", "")
    secret = os.environ.get("UPLAND_SECRET", "")
    if not app_id or not secret:
        sys.exit("[listings] Missing UPLAND_APP_ID / UPLAND_SECRET in .env")
    token = base64.b64encode(f"{app_id}:{secret}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
        "User-Agent": "UplandScopeListings/1.0",
    }


def _on_signal(signum, _frame) -> None:
    global _stop
    _stop = True
    print("\n[listings] stopping after the current request...", flush=True)


def dev_get(path: str, params: dict | None = None, attempts: int = 5) -> dict:
    """GET the developers API. 409s back off the same way neighborhood_map does."""
    url = f"{DEV_API}{path}"
    last_error: Exception | None = None
    for attempt in range(attempts):
        if _stop:
            raise CrawlStopped()
        try:
            response = requests.get(url, headers=_auth_headers(), params=params, timeout=30)
            if response.status_code == 409:
                time.sleep((attempt + 1) * 2)
                continue
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            last_error = exc
            if attempt == attempts - 1:
                break
            time.sleep(2 ** attempt)
    raise ApiError(f"{path} failed: {last_error}")


def public_price(prop_id, delay: float, gate: dict) -> dict:
    """Price + currency from the public property endpoint.

    state is 'ok', 'gone' (404), or 'unknown' (timeout, 409, no price).
    unknown keeps any price already stored. gone lets a finished scan drop it.
    """
    with gate["lock"]:
        wait = delay - (time.monotonic() - gate["last"])
        if wait > 0:
            time.sleep(wait)
        gate["last"] = time.monotonic()
    try:
        response = requests.get(
            PUBLIC_PROP.format(prop_id=prop_id),
            headers={"User-Agent": "UplandScopeListings/1.0"},
            timeout=15,
        )
    except requests.RequestException:
        return {"state": "unknown"}
    if response.status_code == 404:
        return {"state": "gone"}
    if response.status_code != 200:
        return {"state": "unknown"}
    try:
        data = response.json()
    except ValueError:
        return {"state": "unknown"}
    # "Owned" / "Locked" / anything else means it is not a current listing.
    # A missing status with no on_market block is treated as unknown so a
    # blip does not wipe a price we already stored.
    status = data.get("status") or ""
    if status and status != "For sale":
        return {"state": "gone"}
    on_market = data.get("on_market") or {}
    currency = on_market.get("currency") or ""
    price = data.get("price")
    if not currency or price is None:
        return {"state": "unknown"}
    return {
        "state": "ok",
        "currency": currency,
        "price": price,
        "owner": data.get("owner_username") or "",
    }


def _record_for_sale(conn, props: list[dict], *, city_id: int, city_name: str,
                      neighborhood: str | None, seen_at: str, workers: int, delay: float) -> int:
    for_sale = []
    for prop in props:
        if prop.get("status") != "For sale":
            continue
        hood_obj = prop.get("neighborhood") or {}
        hood_name = hood_obj.get("name", "") if isinstance(hood_obj, dict) else str(hood_obj or "")
        if neighborhood and fold(hood_name) != fold(neighborhood):
            continue
        for_sale.append((prop, hood_name))
    if not for_sale:
        return 0

    gate = {"lock": threading.Lock(), "last": 0.0}

    def fetch(item):
        prop, hood_name = item
        if _stop:
            return prop, hood_name, {"state": "unknown"}
        return prop, hood_name, public_price(prop.get("id"), delay, gate)

    kept = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for prop, hood_name, price in pool.map(fetch, for_sale):
            if price["state"] == "gone":
                continue
            city_obj = prop.get("city") or {}
            listed_city = city_obj.get("name") if isinstance(city_obj, dict) else ""
            upsert_listing(conn, {
                "property_id": prop.get("id"),
                "address": prop.get("address") or "",
                "city_id": city_id,
                "city": listed_city or city_name,
                "neighborhood": hood_name,
                "mint_price": prop.get("mintPrice"),
                "last_seen": seen_at,
                "price": price.get("price") if price["state"] == "ok" else None,
                "currency": price.get("currency") if price["state"] == "ok" else None,
                "owner_username": price.get("owner") if price["state"] == "ok" else None,
            })
            kept += 1
    return kept


def _fetch_page(city_id: int, page: int, text_search: str | None) -> dict:
    params = {"cityId": city_id, "currentPage": page, "pageSize": PAGE_SIZE}
    if text_search:
        params["textSearch"] = text_search
    return dev_get("/properties", params)


def street_terms(cache_city: str, neighborhood: str) -> list[str]:
    if not PROP_CACHE_DB.exists():
        return []
    conn = sqlite3.connect(f"file:{PROP_CACHE_DB}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT address FROM properties WHERE neighborhood = ? AND LOWER(city) = LOWER(?)",
            (neighborhood.upper(), cache_city),
        ).fetchall()
    finally:
        conn.close()
    terms = {street_search_term(address) for (address,) in rows}
    return sorted(term for term in terms if term)


def _owned_ids(username: str) -> list[str]:
    path = BLOCKCHAIN_CACHE / f"{username.lower()}_blockchain_cache.json"
    if not path.exists():
        sys.exit(
            f"[listings] No portfolio cache at {path}. "
            "Generate a neighborhood map for this user once, or pass --city."
        )
    data = json.loads(path.read_text())
    return [str(prop_id) for prop_id in data.get("owned") or []]


def _owned_places(prop_ids: list[str]) -> list[dict]:
    if not PROP_CACHE_DB.exists():
        sys.exit(f"[listings] Missing {PROP_CACHE_DB}")
    conn = sqlite3.connect(f"file:{PROP_CACHE_DB}?mode=ro", uri=True)
    places: dict[str, set[str]] = {}
    try:
        for start in range(0, len(prop_ids), 900):
            chunk = prop_ids[start:start + 900]
            marks = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT city, neighborhood FROM properties WHERE prop_id IN ({marks})",
                chunk,
            ).fetchall()
            for city, neighborhood in rows:
                city_name = (city or "").strip()
                if not city_name:
                    continue
                places.setdefault(city_name, set())
                hood = (neighborhood or "").strip().upper()
                if hood:
                    places[city_name].add(hood)
    finally:
        conn.close()
    return [
        {"cache_city": city, "neighborhoods": sorted(hoods)}
        for city, hoods in sorted(places.items())
    ]


def _api_cities() -> list[dict]:
    payload = dev_get("/cities")
    return payload.get("cities") or []


def _city_total(city_id: int) -> int:
    payload = _fetch_page(city_id, 1, None)
    return int(payload.get("totalResults") or 0)


def build_queue(args) -> list[dict]:
    cities = _api_cities()
    if args.city:
        match = match_city(args.city, cities)
        if match is None:
            names = ", ".join(sorted(city["name"] for city in cities)[:8])
            sys.exit(f"[listings] City '{args.city}' not found. Sample: {names}, ...")
        if args.neighborhood:
            return [{
                "key": f"hood:{match['id']}:{args.neighborhood.upper()}",
                "kind": "neighborhood",
                "city_id": match["id"],
                "city": match["name"],
                "cache_city": args.city,
                "neighborhood": args.neighborhood.upper(),
                "total_props": None,
                "expect_partial": False,
                "note": None,
            }]
        total = _city_total(match["id"])
        owned = [{
            "city_id": match["id"],
            "city": match["name"],
            "cache_city": args.city,
            "neighborhood": None,
        }]
        # An explicit --city with no neighborhood still refuses to pretend a
        # huge city was fully scanned. Street targets need a portfolio.
        return plan_targets(
            owned, {match["id"]: total},
            max_city_props=args.max_city_props, include_large=args.include_large,
        )

    prop_ids = _owned_ids(args.username)
    places = _owned_places(prop_ids)
    print(f"[listings] {len(prop_ids)} owned ids, {len(places)} cache cities", flush=True)
    owned_rows = []
    sizes: dict[int, int] = {}
    for place in places:
        if _stop:
            raise CrawlStopped()
        match = match_city(place["cache_city"], cities)
        if match is None:
            print(f"[listings] no API city for cache name '{place['cache_city']}'", flush=True)
            continue
        print(f"[listings] sizing {match['name']}...", flush=True)
        sizes[match["id"]] = _city_total(match["id"])
        time.sleep(args.delay)
        for hood in place["neighborhoods"] or [None]:
            owned_rows.append({
                "city_id": match["id"],
                "city": match["name"],
                "cache_city": place["cache_city"],
                "neighborhood": hood,
            })
    return plan_targets(
        owned_rows, sizes,
        max_city_props=args.max_city_props, include_large=args.include_large,
    )


def _print_queue(targets: list[dict]) -> None:
    crawlable = [t for t in targets if t["kind"] != "deferred"]
    deferred = [t for t in targets if t["kind"] == "deferred"]
    print(f"[listings] {len(crawlable)} crawl targets, {len(deferred)} deferred cities")
    for target in targets:
        scope = target["city"]
        if target.get("neighborhood"):
            scope = f"{target['neighborhood']}, {target['city']}"
        total = target.get("total_props")
        size = f"{total:,} props" if total is not None else "size n/a"
        print(f"  {target['kind']:12} {size:>16}  {scope}")
        if target.get("note"):
            print(f"               {target['note']}")


def _job_prefix(args) -> str:
    if args.neighborhood and args.city:
        return f"job:hood:{fold(args.city)}:{fold(args.neighborhood)}"
    if args.city:
        return f"job:city:{fold(args.city)}"
    return "job:owned"


def _load_job(conn, prefix: str) -> tuple[list[dict], int, int, str | None]:
    raw = get_state(conn, f"{prefix}:queue")
    if not raw:
        return [], 0, 0, None
    return (
        json.loads(raw),
        int(get_state(conn, f"{prefix}:index", "0") or 0),
        int(get_state(conn, f"{prefix}:unit", "0") or 0),
        get_state(conn, f"{prefix}:started_at"),
    )


def _save_job(conn, prefix: str, targets: list[dict], index: int, unit: int, started_at: str | None) -> None:
    set_state(conn, f"{prefix}:queue", json.dumps(targets))
    set_state(conn, f"{prefix}:index", str(index))
    set_state(conn, f"{prefix}:unit", str(unit))
    set_state(conn, f"{prefix}:started_at", started_at or "")
    conn.commit()


def _finish_target(conn, target: dict, *, started_at: str, status: str, units_done: int,
                   units_total: int, note: str | None) -> None:
    removed = 0
    if status == "complete":
        removed = delete_unseen(
            conn,
            city_id=target["city_id"],
            neighborhood=target.get("neighborhood"),
            started_at=started_at,
        )
    listing_count = count_listings(
        conn, city_id=target["city_id"], neighborhood=target.get("neighborhood"),
    )
    save_target(conn, {
        **target,
        "status": status,
        "started_at": started_at,
        "finished_at": utc_now() if status != "running" else None,
        "units_done": units_done,
        "units_total": units_total,
        "listing_count": listing_count,
        "note": note if note is not None else target.get("note"),
    })
    conn.commit()
    label = target.get("neighborhood") or target["city"]
    if status == "complete":
        print(f"[listings] {label} complete: {listing_count} listings, removed {removed} stale", flush=True)
    else:
        print(f"[listings] {label} {status}: {listing_count} listings ({note or 'paused'})", flush=True)


def _crawl_pages(conn, target: dict, *, started_at: str, start_page: int, args) -> str:
    """Scan a city. Returns 'done', 'partial', or 'stopped'."""
    total = target.get("total_props") or 0
    pages_total = PAGE_CAP
    page = max(1, start_page)
    pages_fetched_this_run = 0
    hit_cap = bool(target.get("expect_partial"))
    try:
        while page <= PAGE_CAP:
            if _stop:
                raise CrawlStopped()
            payload = _fetch_page(target["city_id"], page, None)
            results = payload.get("results") or []
            total = int(payload.get("totalResults") or total or 0)
            pages_total = max(1, min(PAGE_CAP, math.ceil(total / PAGE_SIZE))) if total else page
            if total > FULL_CITY_MAX_PROPS or total > args.max_city_props:
                hit_cap = True
            if (
                page == max(1, start_page)
                and total > args.max_city_props
                and not args.include_large
                and not target.get("expect_partial")
            ):
                _finish_target(
                    conn, {**target, "total_props": total},
                    started_at=started_at, status="deferred",
                    units_done=0, units_total=0,
                    note=(
                        f"{total:,} properties exceeds {args.max_city_props:,}; "
                        "not page-scanning. Pass --include-large for a partial scan, "
                        "or --neighborhood for a street search."
                    ),
                )
                return "done"
            seen_at = utc_now()
            priced = _record_for_sale(
                conn, results,
                city_id=target["city_id"], city_name=target["city"],
                neighborhood=None, seen_at=seen_at,
                workers=args.workers, delay=args.delay,
            )
            print(
                f"[listings] {target['city']} page {page}/{pages_total} "
                f"— {priced} for-sale priced",
                flush=True,
            )
            _save_job_progress(conn, args, page)
            pages_fetched_this_run += 1
            reached_end = (not results) or len(results) < PAGE_SIZE or total <= page * PAGE_SIZE
            if page == 1 and not results and not total:
                _finish_target(
                    conn, {**target, "total_props": 0},
                    started_at=started_at, status="error",
                    units_done=1, units_total=1,
                    note="API returned no properties; unseen listings kept",
                )
                return "done"
            capped_debug = bool(args.max_pages) and pages_fetched_this_run >= args.max_pages
            if reached_end or capped_debug or page == PAGE_CAP:
                complete = (
                    reached_end
                    and not hit_cap
                    and not capped_debug
                    and total <= FULL_CITY_MAX_PROPS
                    and total <= args.max_city_props
                )
                status = "complete" if complete else "partial"
                note = target.get("note")
                if capped_debug:
                    note = f"stopped at --max-pages {args.max_pages}; unseen listings kept"
                elif hit_cap and not reached_end:
                    note = (
                        f"stopped at API page cap ({PAGE_CAP}); "
                        f"city has {total:,} properties; unseen listings kept"
                    )
                elif hit_cap:
                    note = f"city has {total:,} properties, over the full-scan cap; unseen listings kept"
                _finish_target(
                    conn, {**target, "total_props": total},
                    started_at=started_at, status=status,
                    units_done=page, units_total=pages_total, note=note,
                )
                return "done" if status == "complete" else "partial"
            page += 1
            time.sleep(args.delay)
    except CrawlStopped:
        save_target(conn, {
            **target,
            "status": "running",
            "started_at": started_at,
            "units_done": max(0, page - 1),
            "units_total": pages_total,
            "total_props": total,
            "note": "paused",
        })
        conn.commit()
        print(f"[listings] paused {target['city']} at page {page}", flush=True)
        return "stopped"
    except ApiError as exc:
        save_target(conn, {
            **target, "status": "error", "started_at": started_at,
            "units_done": max(0, page - 1), "units_total": pages_total,
            "note": str(exc),
        })
        conn.commit()
        print(f"[listings] {target['city']} stopped: {exc}", flush=True)
        return "stopped"
    return "partial"


def _crawl_neighborhood(conn, target: dict, *, started_at: str, start_unit: int, args) -> str:
    terms = street_terms(target.get("cache_city") or target["city"], target["neighborhood"])
    if not terms:
        _finish_target(
            conn, target, started_at=started_at, status="error",
            units_done=0, units_total=0,
            note="no street names in property_cache.db for this neighborhood",
        )
        return "done"
    hit_cap = False
    index = start_unit
    try:
        while index < len(terms):
            if _stop:
                raise CrawlStopped()
            term = terms[index]
            page = 1
            while page <= PAGE_CAP:
                payload = _fetch_page(target["city_id"], page, term)
                results = payload.get("results") or []
                total = int(payload.get("totalResults") or 0)
                seen_at = utc_now()
                priced = _record_for_sale(
                    conn, results,
                    city_id=target["city_id"], city_name=target["city"],
                    neighborhood=target["neighborhood"], seen_at=seen_at,
                    workers=args.workers, delay=args.delay,
                )
                print(
                    f"[listings] {target['neighborhood']} street {index + 1}/{len(terms)} "
                    f"'{term}' page {page} — {priced} for-sale",
                    flush=True,
                )
                reached_end = (not results) or len(results) < PAGE_SIZE or total <= page * PAGE_SIZE
                if reached_end:
                    break
                if page == PAGE_CAP:
                    hit_cap = True
                    print(f"[listings] '{term}' hit the {PAGE_CAP}-page cap", flush=True)
                    break
                page += 1
                time.sleep(args.delay)
            index += 1
            _save_job_progress(conn, args, index)
            if args.max_pages and index >= args.max_pages:
                _finish_target(
                    conn, target, started_at=started_at, status="partial",
                    units_done=index, units_total=len(terms),
                    note=f"stopped at --max-pages {args.max_pages}; unseen listings kept",
                )
                return "partial"
        status = "partial" if hit_cap else "complete"
        note = target.get("note")
        if hit_cap:
            note = "a street search hit the API page cap; unseen listings kept"
        _finish_target(
            conn, target, started_at=started_at, status=status,
            units_done=index, units_total=len(terms), note=note,
        )
        return "done" if status == "complete" else "partial"
    except CrawlStopped:
        save_target(conn, {
            **target,
            "status": "running",
            "started_at": started_at,
            "units_done": index,
            "units_total": len(terms),
            "note": "paused",
        })
        conn.commit()
        print(f"[listings] paused {target['neighborhood']} at street {index + 1}", flush=True)
        return "stopped"
    except ApiError as exc:
        save_target(conn, {
            **target, "status": "error", "started_at": started_at,
            "units_done": index, "units_total": len(terms), "note": str(exc),
        })
        conn.commit()
        print(f"[listings] {target['neighborhood']} stopped: {exc}", flush=True)
        return "stopped"


def _save_job_progress(conn, args, unit: int) -> None:
    """Persist the in-target cursor. The target index is updated by the runner."""
    prefix = _job_prefix(args)
    index = int(get_state(conn, f"{prefix}:index", "0") or 0)
    started = get_state(conn, f"{prefix}:started_at")
    raw = get_state(conn, f"{prefix}:queue")
    if raw:
        set_state(conn, f"{prefix}:unit", str(unit))
        set_state(conn, f"{prefix}:index", str(index))
        set_state(conn, f"{prefix}:started_at", started or "")
        conn.commit()


def _should_skip(conn, target: dict, args) -> bool:
    if args.force or args.max_pages:
        return False
    if target["kind"] == "deferred":
        return False
    return target_is_fresh(conn, target["key"], args.max_age_hours)


def run_queue(conn, args, targets: list[dict], start_index: int, start_unit: int,
              started_at: str | None) -> None:
    prefix = _job_prefix(args)
    for index, target in enumerate(targets):
        if index < start_index:
            continue
        if target["kind"] == "deferred":
            save_target(conn, {**target, "status": "deferred"})
            conn.commit()
            continue
        if _should_skip(conn, target, args):
            print(f"[listings] skip fresh {target.get('neighborhood') or target['city']}", flush=True)
            continue
        unit = start_unit if index == start_index else 0
        if unit == 0 or not started_at:
            started_at = utc_now()
        _save_job(conn, prefix, targets, index, unit, started_at)
        save_target(conn, {**target, "status": "running", "started_at": started_at, "units_done": unit})
        conn.commit()
        if target["kind"] == "city":
            # unit is the last completed page; resume at the next one.
            outcome = _crawl_pages(
                conn, target, started_at=started_at, start_page=unit + 1, args=args,
            )
        elif target["kind"] == "neighborhood":
            outcome = _crawl_neighborhood(
                conn, target, started_at=started_at, start_unit=unit, args=args,
            )
        else:
            continue
        if outcome == "stopped" or _stop or args.max_pages:
            return
        started_at = None
        start_unit = 0
    _save_job(conn, prefix, targets, len(targets), 0, "")
    print("[listings] queue finished", flush=True)


def _place_name(value) -> str:
    if isinstance(value, dict):
        return value.get("name") or ""
    return str(value or "")


def _place_id(value):
    if isinstance(value, dict):
        return value.get("id")
    return None


def collect_cached_forsale(roots: list[Path] | None = None) -> list[dict]:
    """For-sale rows already stored in local property caches.

    These caches are whatever a previous map run saved. They are not a live
    city scan. Callers recheck each id against the public API before inserting.
    """
    if roots is None:
        roots = [ROOT / "optimizer" / "cache", ROOT / "webapp" / "cache" / "neighborhoods"]
    found: dict[str, dict] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in root.glob("*_props_cache.json"):
            try:
                payload = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, list):
                continue
            for prop in payload:
                if not isinstance(prop, dict) or prop.get("status") != "For sale":
                    continue
                prop_id = str(prop.get("id") or prop.get("prop_id") or "")
                if not prop_id:
                    continue
                seed = {
                    "property_id": prop_id,
                    "address": prop.get("address") or prop.get("full_address") or "",
                    "city_id": _place_id(prop.get("city")),
                    "city": _place_name(prop.get("city")),
                    "neighborhood": _place_name(prop.get("neighborhood")),
                    "mint_price": prop.get("mintPrice"),
                }
                prev = found.get(prop_id)
                if prev is None or (not prev.get("mint_price") and seed["mint_price"]):
                    found[prop_id] = seed
    return list(found.values())


def reprice_caches(conn, args) -> None:
    """Confirm cached for-sale ids against the public API and store the ones still listed.

    The developers list API is not required. Coverage is stored as partial on
    purpose: this is not a complete city, so collection search must keep
    doing its live scan.
    """
    seeds = collect_cached_forsale()
    print(f"[listings] rechecking {len(seeds)} cached for-sale ids", flush=True)
    if not seeds:
        print("[listings] no cached for-sale properties found")
        return
    gate = {"lock": threading.Lock(), "last": 0.0}
    kept = gone = unknown = 0
    seen_at = utc_now()

    def fetch(seed):
        if _stop:
            return seed, {"state": "unknown"}
        return seed, public_price(seed["property_id"], args.delay, gate)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for index, (seed, price) in enumerate(pool.map(fetch, seeds), 1):
            if price["state"] == "ok":
                upsert_listing(conn, {
                    **seed,
                    "last_seen": seen_at,
                    "price": price["price"],
                    "currency": price["currency"],
                    "owner_username": price["owner"],
                })
                kept += 1
            elif price["state"] == "gone":
                gone += 1
            else:
                unknown += 1
            if index % 25 == 0 or index == len(seeds):
                conn.commit()
                print(
                    f"[listings] {index}/{len(seeds)}  still listed {kept}  "
                    f"not listed {gone}  unknown {unknown}",
                    flush=True,
                )
            if _stop:
                break
    save_target(conn, {
        "key": "reprice:props-cache",
        "kind": "reprice",
        "city": "cached neighborhoods",
        "status": "partial",
        "started_at": seen_at,
        "finished_at": utc_now(),
        "units_done": kept + gone + unknown,
        "units_total": len(seeds),
        "listing_count": kept,
        "note": (
            f"Rechecked {len(seeds)} cached for-sale ids via the public API; "
            f"{kept} still listed. Not a full city scan."
        ),
    })
    conn.commit()
    print(f"[listings] stored {kept} current listings", flush=True)


def cmd_stats(conn) -> None:
    info = summary(conn)
    print(
        f"listings {info['listings']}  (UPX {info['upx']}, USD {info['usd']})  "
        f"newest listing {info['freshest_listing'] or '—'}  "
        f"newest complete crawl {info['freshest_crawl'] or '—'}"
    )
    if info["targets"]:
        print("targets " + ", ".join(f"{status} {count}" for status, count in sorted(info["targets"].items())))
    for row in coverage_rows(conn):
        scope = row["city"] or ""
        if row["neighborhood"]:
            scope = f"{row['neighborhood']}, {scope}"
        done = row["units_done"] or 0
        total = row["units_total"] or 0
        progress = f"{done}/{total}" if total else ""
        print(f"  {row['status']:10} {progress:>8}  {scope}  {row['note'] or ''}")


def cmd_query(conn, args) -> None:
    currency = None if args.currency == "any" else args.currency
    rows = search_listings(
        conn,
        currency=currency,
        city=args.city,
        neighborhood=args.neighborhood,
        sort=args.sort,
        limit=args.limit,
    )
    if not rows:
        print("[listings] no rows. The snapshot is empty or the filter matched nothing.")
        return
    for row in rows:
        markup = f"{row['markup_pct']:+.1f}%" if row["markup_pct"] is not None else "    n/a"
        price = f"{row['price']:,.0f}" if row["price"] is not None else "     ?"
        print(
            f"{markup:>8}  {price:>8} {row['currency'] or '—':3}  "
            f"{row['address'][:32]:32}  {(row['neighborhood'] or '')[:18]:18}  {row['city']}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--query", action="store_true")
    parser.add_argument("--currency", default="UPX", help="UPX, USD, or any (query only)")
    parser.add_argument("--sort", default="markup", choices=("markup", "price", "seen"))
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--city", help="Crawl or filter this city only")
    parser.add_argument("--neighborhood", help="Crawl or filter this neighborhood; crawl requires --city")
    parser.add_argument("--username", default="pugs08")
    parser.add_argument(
        "--max-pages", type=int, default=0,
        help="Stop after this many city pages, or this many streets for a neighborhood. Keeps unseen listings.",
    )
    parser.add_argument("--max-city-props", type=int, default=FULL_CITY_MAX_PROPS)
    parser.add_argument("--include-large", action="store_true",
                        help="Page cities over the cap anyway, marked partial. Does not delete unseen rows.")
    parser.add_argument("--rebuild", action="store_true", help="Discard the saved queue and plan again")
    parser.add_argument("--force", action="store_true", help="Recrawl targets that are still inside the freshness window")
    parser.add_argument("--max-age-hours", type=float, default=24)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--delay", type=float, default=0.2)
    parser.add_argument("--dry-queue", action="store_true")
    parser.add_argument(
        "--reprice-caches", action="store_true",
        help="Recheck for-sale ids already in local property caches via the public API.",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    args = parser.parse_args()

    if args.neighborhood and not args.city and not (args.stats or args.query):
        sys.exit("[listings] --neighborhood requires --city when crawling")
    args.workers = min(max(args.workers, 1), 3)
    args.delay = max(args.delay, 0.05)
    if args.max_city_props > FULL_CITY_MAX_PROPS and not args.include_large:
        print(
            f"[listings] --max-city-props {args.max_city_props} is above the API page cap "
            f"of {FULL_CITY_MAX_PROPS}. Full scans still stop being 'complete' at that cap.",
            flush=True,
        )

    conn = connect(Path(args.db))
    try:
        if args.stats:
            cmd_stats(conn)
            return
        if args.query:
            cmd_query(conn, args)
            return
        if args.reprice_caches:
            signal.signal(signal.SIGINT, _on_signal)
            signal.signal(signal.SIGTERM, _on_signal)
            reprice_caches(conn, args)
            return

        _load_env()
        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)
        prefix = _job_prefix(args)
        targets, index, unit, started_at = _load_job(conn, prefix)
        if args.rebuild or args.dry_queue or not targets:
            targets = build_queue(args)
            index, unit, started_at = 0, 0, None
            if not args.dry_queue:
                _save_job(conn, prefix, targets, 0, 0, "")
        if args.dry_queue:
            _print_queue(targets)
            return
        _print_queue(targets)
        run_queue(conn, targets=targets, args=args, start_index=index, start_unit=unit, started_at=started_at)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
