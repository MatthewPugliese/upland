"""
UplandScope — Portfolio Analyzer

Given a username + EOS account, builds a full portfolio breakdown:
summary (properties, mint value, UP², % developed), neighborhood
breakdown, structure inventory, and an undeveloped-properties list.

Reuses the same blockchain-derived ownership lookup as the collection
optimizer (`load_user_properties`), then fetches UP² + placed structures
per property from the public Upland API in one pass.

Yield figures are the same flat-rate estimate used elsewhere in this repo
(mintPrice x assumed annual yield rate) — there is no real per-property
yield data available yet (see docs/PORTFOLIO_ANALYZER_PLAN.md and
docs/ECONOMY_DASHBOARD_PLAN.md for why: n31 yield-collection events are
not ingested by the scraper). Treat est_hourly_yield / est_monthly_yield
as a rough portfolio-level estimate only, not a per-property ranking —
under a flat rate every property yields strictly proportional to mint
price, so there's nothing to rank.

Spark investment (total_sparks_invested) IS a real, computed figure —
sum of optimizer.spark_estimator.get_spark_cost()'s total_sparks per
placed structure, the same construction-cost model used by the spark
estimator elsewhere. Some structure types only have an estimated cost
(no exact measured totalSparksRequired sample) — pct_sparks_estimated
tells you what fraction of the sum is estimate vs measured.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
import concurrent.futures
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / "cache" / "portfolio"
OPTIMIZER_DIR = SCRIPT_DIR.parent / "optimizer"
sys.path.insert(0, str(OPTIMIZER_DIR))

from structure_fitter import STRUCTURES  # noqa: E402
from score_calculator import _lookup as _structure_lookup  # noqa: E402
from spark_estimator import get_spark_cost  # noqa: E402

_DETAILS_TTL = 86400  # 24h — matches get_upland_property_structures' convention
_MARKET_VALUE_MAX_NEIGHBORHOODS = 15  # cap comp-search fanout for the optional market-value estimate


def _safe_name(username: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in username.lower())


def fetch_property_details(props: list, cache_path: Path) -> dict:
    """
    Fetch UP² + placed buildings for each property, one API call per property
    (the public endpoint returns both `area` and `buildings` together, so a
    single fetch covers what get_upland_property_structures/fetch_api_dims
    would otherwise split into two separate passes).

    Returns {prop_id_str: {"up2": float|None, "buildings": [...]}}.
    """
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            age = time.time() - cached.get("_ts", 0)
            if age < _DETAILS_TTL:
                return {k: v for k, v in cached.items() if k != "_ts"}
        except Exception:
            pass

    results: dict = {}

    def _fetch_one(prop: dict) -> tuple[str, dict]:
        pid = str(prop["id"])
        try:
            req = urllib.request.Request(
                f"https://api.upland.me/properties/{pid}",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            return pid, {"up2": data.get("area"), "buildings": data.get("buildings") or []}
        except Exception:
            return pid, {"up2": None, "buildings": []}

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(_fetch_one, p) for p in props]
        for future in concurrent.futures.as_completed(futures):
            pid, details = future.result()
            results[pid] = details

    payload = dict(results)
    payload["_ts"] = time.time()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload))
    return results


def _estimate_market_value(neighborhoods: list, hood_city: dict) -> dict:
    """
    Estimate current market value for the top N neighborhoods by mint value
    (comp search is too slow to run per-neighborhood across an entire
    portfolio — see docs/PORTFOLIO_ANALYZER_PLAN.md). Applies each covered
    neighborhood's median UPX/UP² and USD/UP² (from valuation.py's comp
    search) against the UP² the portfolio holds there.

    Mutates nothing; returns {"total_upx": float, "total_usd": float,
    "covered_mint": int, "covered_count": int, "rates": {hood: {...}}}.
    Neighborhoods beyond the cap, or with no comps at all, are excluded from
    the total and reflected in covered_mint/covered_count vs. the full totals.
    """
    from valuation import neighborhood_valuation_rate

    targets = neighborhoods[:_MARKET_VALUE_MAX_NEIGHBORHOODS]
    rates: dict = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(neighborhood_valuation_rate, h["neighborhood"], hood_city.get(h["neighborhood"], "")): h
            for h in targets
        }
        for future in concurrent.futures.as_completed(futures):
            h = futures[future]
            try:
                rates[h["neighborhood"]] = future.result()
            except Exception:
                rates[h["neighborhood"]] = None

    total_upx = 0.0
    total_usd = 0.0
    covered_mint = 0
    covered_count = 0
    for h in targets:
        rate = rates.get(h["neighborhood"])
        if not rate:
            continue
        if rate.get("upx_per_up2"):
            total_upx += rate["upx_per_up2"] * h["up2"]
        if rate.get("usd_per_up2"):
            total_usd += rate["usd_per_up2"] * h["up2"]
        if rate.get("upx_per_up2") or rate.get("usd_per_up2"):
            covered_mint += h["mint"]
            covered_count += 1

    return {
        "total_upx": round(total_upx),
        "total_usd": round(total_usd, 2),
        "covered_count": covered_count,
        "attempted_count": len(targets),
        "covered_mint": covered_mint,
        "rates": rates,
    }


def build_portfolio(username: str, eos_account: str | None = None, annual_rate: float = 0.1225,
                     estimate_market_value: bool = False) -> dict:
    """
    Build the full portfolio breakdown for a username.

    eos_account is optional — if omitted, it's resolved from the reverse
    username->EOS index (see username_lookup.lookup_eos_account), which
    enables "scouting another player" with just their Upland username.
    Returns an error dict if the account can't be resolved either way.

    estimate_market_value opts into an extra comp-search pass (via
    valuation.neighborhood_valuation_rate) against the top 15 neighborhoods
    by mint value — slow (one comp search + area lookups per neighborhood),
    so it's off by default and capped rather than run across every
    neighborhood in the portfolio.

    Returns a dict with: summary, neighborhoods (list), structures (list),
    undeveloped (list of property dicts with 0 buildings), properties
    (full enriched list, sorted by mint value desc).
    """
    from collection_optimizer import load_user_properties

    if not eos_account:
        from username_lookup import lookup_eos_account
        eos_account = lookup_eos_account(username)
        if not eos_account:
            return {
                "error": f"Couldn't resolve an EOS account for '{username}' — "
                         f"it isn't in the known username cache. Provide the EOS account directly.",
                "properties": [],
            }

    props = load_user_properties(username, eos_account)
    if not props:
        return {"error": f"No properties found for {username}.", "properties": []}

    safe = _safe_name(username)
    details_cache = CACHE_DIR / f"{safe}_details_cache.json"
    details = fetch_property_details(props, details_cache)

    total_mint = 0
    total_up2 = 0.0
    developed_count = 0
    struct_counts: dict = defaultdict(lambda: {"count": 0, "su": 0.0, "sparks": 0})
    total_sparks = 0
    total_sparks_buildings = 0
    total_sparks_estimated_buildings = 0
    hood_stats: dict = defaultdict(lambda: {"count": 0, "mint": 0, "up2": 0.0, "developed": 0})
    hood_city: dict = {}
    undeveloped = []
    enriched_props = []

    for p in props:
        pid = p["id"]
        d = details.get(pid) or {"up2": None, "buildings": []}
        up2 = d.get("up2") or 0
        buildings = d.get("buildings") or []
        mint = p.get("mintPrice") or 0
        hood = p.get("neighborhood") or "Unknown"
        is_developed = len(buildings) > 0

        total_mint += mint
        total_up2 += up2
        if is_developed:
            developed_count += 1
        else:
            undeveloped.append(p)

        for b in buildings:
            name = b.get("buildingName") or b.get("buildingType") or "Unknown"
            info = _structure_lookup(name)
            struct_counts[name]["count"] += 1
            struct_counts[name]["su"] += info.get("su", 0) or 0

            spark_cost = get_spark_cost(name)
            if spark_cost:
                struct_counts[name]["sparks"] += spark_cost["total_sparks"]
                total_sparks += spark_cost["total_sparks"]
                total_sparks_buildings += 1
                if spark_cost["estimated"]:
                    total_sparks_estimated_buildings += 1

        hs = hood_stats[hood]
        hs["count"] += 1
        hs["mint"] += mint
        hs["up2"] += up2
        if is_developed:
            hs["developed"] += 1
        hood_city.setdefault(hood, p.get("city") or "")

        enriched_props.append({**p, "up2": up2, "structure_count": len(buildings), "developed": is_developed})

    total_props = len(props)
    pct_developed = round(100 * developed_count / total_props, 1) if total_props else 0.0
    est_monthly_yield = round(total_mint * annual_rate / 12)
    est_hourly_yield = round(est_monthly_yield / (30 * 24), 2)

    neighborhoods = [
        {
            "neighborhood": hood,
            "count": s["count"],
            "mint": round(s["mint"]),
            "up2": round(s["up2"], 1),
            "developed": s["developed"],
            "pct_developed": round(100 * s["developed"] / s["count"], 1) if s["count"] else 0,
        }
        for hood, s in sorted(hood_stats.items(), key=lambda kv: -kv[1]["mint"])
    ]

    structures = [
        {"name": name, "count": s["count"], "su": round(s["su"], 1), "sparks": s["sparks"]}
        for name, s in sorted(struct_counts.items(), key=lambda kv: -kv[1]["count"])
    ]
    pct_sparks_estimated = (
        round(100 * total_sparks_estimated_buildings / total_sparks_buildings, 1)
        if total_sparks_buildings else 0.0
    )

    market_value = None
    if estimate_market_value and neighborhoods:
        market_value = _estimate_market_value(neighborhoods, hood_city)
        for h in neighborhoods:
            rate = market_value["rates"].get(h["neighborhood"])
            if rate:
                h["market_upx_per_up2"] = rate.get("upx_per_up2")
                h["market_usd_per_up2"] = rate.get("usd_per_up2")

    summary = {
        "total_properties": total_props,
        "total_mint_value": round(total_mint),
        "total_up2": round(total_up2, 1),
        "pct_developed": pct_developed,
        "developed_count": developed_count,
        "undeveloped_count": total_props - developed_count,
        "est_monthly_yield": est_monthly_yield,
        "est_hourly_yield": est_hourly_yield,
        "total_sparks_invested": total_sparks,
        "pct_sparks_estimated": pct_sparks_estimated,
        "neighborhood_count": len(hood_stats),
        "annual_rate_pct": round(annual_rate * 100, 2),
    }
    if market_value is not None:
        summary["market_value_upx"] = market_value["total_upx"]
        summary["market_value_usd"] = market_value["total_usd"]
        summary["market_value_neighborhoods_covered"] = market_value["covered_count"]
        summary["market_value_neighborhoods_attempted"] = market_value["attempted_count"]
        summary["market_value_coverage_pct"] = (
            round(100 * market_value["covered_mint"] / total_mint, 1) if total_mint else 0.0
        )

    return {
        "summary": summary,
        "neighborhoods": neighborhoods,
        "structures": structures,
        "undeveloped": sorted(undeveloped, key=lambda p: -(p.get("mintPrice") or 0)),
        "properties": sorted(enriched_props, key=lambda p: -(p.get("mintPrice") or 0)),
    }
