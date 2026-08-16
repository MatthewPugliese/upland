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

_DETAILS_TTL = 86400  # 24h — matches get_upland_property_structures' convention


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


def build_portfolio(username: str, eos_account: str | None = None, annual_rate: float = 0.1225) -> dict:
    """
    Build the full portfolio breakdown for a username.

    eos_account is optional — if omitted, it's resolved from the reverse
    username->EOS index (see username_lookup.lookup_eos_account), which
    enables "scouting another player" with just their Upland username.
    Returns an error dict if the account can't be resolved either way.

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
    struct_counts: dict = defaultdict(lambda: {"count": 0, "su": 0.0})
    hood_stats: dict = defaultdict(lambda: {"count": 0, "mint": 0, "up2": 0.0, "developed": 0})
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

        hs = hood_stats[hood]
        hs["count"] += 1
        hs["mint"] += mint
        hs["up2"] += up2
        if is_developed:
            hs["developed"] += 1

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
        {"name": name, "count": s["count"], "su": round(s["su"], 1)}
        for name, s in sorted(struct_counts.items(), key=lambda kv: -kv[1]["count"])
    ]

    return {
        "summary": {
            "total_properties": total_props,
            "total_mint_value": round(total_mint),
            "total_up2": round(total_up2, 1),
            "pct_developed": pct_developed,
            "developed_count": developed_count,
            "undeveloped_count": total_props - developed_count,
            "est_monthly_yield": est_monthly_yield,
            "est_hourly_yield": est_hourly_yield,
            "neighborhood_count": len(hood_stats),
            "annual_rate_pct": round(annual_rate * 100, 2),
        },
        "neighborhoods": neighborhoods,
        "structures": structures,
        "undeveloped": sorted(undeveloped, key=lambda p: -(p.get("mintPrice") or 0)),
        "properties": sorted(enriched_props, key=lambda p: -(p.get("mintPrice") or 0)),
    }
