"""
Multi-collection budget optimizer

Given a UPX budget and a set of near-complete collections (with optional
for-sale listings already fetched), pick a combination of collection
completions that maximizes projected monthly UPX yield gain.

This is a knapsack-style greedy: score each completable-with-listings
collection by yield_gain / acquisition_cost, then fill budget highest
efficiency first. Also evaluates the single best collection for comparison.

Does NOT re-fetch listings — callers pass pre-priced candidates from
forsale_finder (or synthetic estimates).
"""

from __future__ import annotations


def _monthly_yield_gain(owned_mint_sum: float, gap_mint_sum: float,
                        boost: float, annual_rate: float) -> float:
    """
    Monthly UPX gain from completing a collection.

    Completing multiplies (owned + newly acquired) mint by boost instead of 1.0
    on the collection's properties. Using the same model as collection_optimizer:
        monthly = total_mint * annual_rate * (boost - 1) / 12
    """
    if not boost or boost <= 1:
        return 0.0
    total_mint = (owned_mint_sum or 0) + (gap_mint_sum or 0)
    return total_mint * annual_rate * (boost - 1) / 12


def estimate_completion_cost(listings: list[dict], gap: int) -> dict | None:
    """
    Pick the `gap` cheapest UPX-priced listings to cover the collection gap.

    Returns None if no UPX listings exist at all.
    """
    upx = [
        L for L in (listings or [])
        if L.get("currency") == "UPX" and L.get("price_upx") is not None
    ]
    upx.sort(key=lambda L: L["price_upx"])
    if not upx:
        return None
    chosen = upx[:gap]
    partial = len(chosen) < gap
    cost = sum(L["price_upx"] for L in chosen)
    mint = sum(L.get("mint_price") or 0 for L in chosen)
    return {
        "cost_upx": cost,
        "gap_mint_sum": mint,
        "listings_used": len(chosen),
        "gap": gap,
        "partial": partial,
        "chosen": [
            {
                "address": L.get("address"),
                "price_upx": L.get("price_upx"),
                "mint_price": L.get("mint_price"),
                "markup_pct": L.get("markup_pct"),
            }
            for L in chosen
        ],
    }


def score_collection_option(coll: dict, listings: list[dict],
                            annual_rate: float = 0.1225) -> dict | None:
    """
    Build a scored option for one almost-complete collection given live listings.
    Returns None if not actionable (no boost, no listings, zero cost edge cases).
    """
    boost = coll.get("boost") or 1.0
    gap = coll.get("gap") or 0
    if gap <= 0 or boost <= 1:
        return None

    cost_info = estimate_completion_cost(listings, gap)
    if not cost_info or cost_info["cost_upx"] <= 0:
        return None

    monthly = _monthly_yield_gain(
        coll.get("owned_mint_sum") or 0,
        cost_info["gap_mint_sum"],
        boost,
        annual_rate,
    )
    if monthly <= 0:
        return None

    cost = cost_info["cost_upx"]
    payback_days = round(cost / (monthly * 12 / 365), 1) if monthly > 0 else None
    efficiency = monthly / cost  # monthly UPX per UPX spent

    return {
        "id": coll.get("id"),
        "name": coll.get("name"),
        "boost": boost,
        "gap": gap,
        "rarity": coll.get("rarity"),
        "owned_mint_sum": coll.get("owned_mint_sum") or 0,
        "cost_upx": round(cost),
        "gap_mint_sum": round(cost_info["gap_mint_sum"]),
        "monthly_yield_gain": round(monthly, 2),
        "hourly_yield_gain": round(monthly / (30 * 24), 4),
        "payback_days": payback_days,
        "efficiency": round(efficiency, 8),
        "partial": cost_info["partial"],
        "listings_used": cost_info["listings_used"],
        "chosen_listings": cost_info["chosen"],
        "reward": coll.get("reward") or 0,
    }


def _pack_score(chosen: list[dict]) -> tuple:
    """Sort key: maximize monthly yield, then minimize spend."""
    monthly = sum(o["monthly_yield_gain"] for o in chosen)
    spent = sum(o["cost_upx"] for o in chosen)
    return (monthly, -spent)


def _best_subset(viable: list[dict], budget_upx: float) -> list[dict]:
    """
    Exact subset search for small n (≤14), else density-greedy with single/pair polish.

    Near-complete collections with live listings rarely exceed a dozen, so exact
    search is the common path.
    """
    n = len(viable)
    if n == 0:
        return []

    if n <= 14:
        best: list[dict] = []
        best_key = (0.0, 0.0)
        # bit mask enumeration
        for mask in range(1, 1 << n):
            chosen = []
            spent = 0
            ok = True
            for i in range(n):
                if mask & (1 << i):
                    cost = viable[i]["cost_upx"]
                    if spent + cost > budget_upx:
                        ok = False
                        break
                    spent += cost
                    chosen.append(viable[i])
            if not ok or not chosen:
                continue
            key = _pack_score(chosen)
            if key > best_key:
                best_key = key
                best = chosen
        return best

    # Large n fallback: greedy by efficiency, then try swapping in best single/pair
    ordered = sorted(viable, key=lambda o: (o.get("partial", False), -o.get("efficiency", 0)))
    chosen, spent, used = [], 0, set()
    for o in ordered:
        cid = o.get("id")
        if cid in used or spent + o["cost_upx"] > budget_upx:
            continue
        chosen.append(o)
        spent += o["cost_upx"]
        used.add(cid)

    candidates = [chosen]
    # best single
    singles = [o for o in viable if o["cost_upx"] <= budget_upx]
    if singles:
        candidates.append([max(singles, key=lambda o: o["monthly_yield_gain"])])
    # best pair
    for i, a in enumerate(viable):
        for b in viable[i + 1:]:
            if a["cost_upx"] + b["cost_upx"] <= budget_upx and a.get("id") != b.get("id"):
                candidates.append([a, b])

    return max(candidates, key=_pack_score)


def optimize_budget(options: list[dict], budget_upx: float) -> dict:
    """
    Pick a subset of collections that fit the UPX budget and maximizes
    projected monthly yield gain (tie-break: lower spend).

    Uses exact subset search when ≤14 viable options; otherwise a greedy
    + single/pair polish. Collections are treated as independent (no shared
    property conflict model).

    Also returns the single best collection for comparison.
    """
    viable = [o for o in options if o and o.get("cost_upx") and o["cost_upx"] <= budget_upx]
    # Prefer non-partial when enumerating ties later via score only on monthly
    viable.sort(key=lambda o: (o.get("partial", False), -o.get("efficiency", 0)))

    chosen = _best_subset(viable, budget_upx)
    spent = sum(o["cost_upx"] for o in chosen)
    total_monthly = sum(o["monthly_yield_gain"] for o in chosen)

    single_best = max(viable, key=lambda o: o["monthly_yield_gain"]) if viable else None

    combo_better = (
        single_best is not None
        and len(chosen) > 1
        and total_monthly > single_best["monthly_yield_gain"]
    )

    return {
        "budget_upx": budget_upx,
        "spent_upx": round(spent),
        "remaining_upx": round(budget_upx - spent),
        "collections": [
            {
                "id": o["id"],
                "name": o["name"],
                "boost": o["boost"],
                "cost_upx": o["cost_upx"],
                "monthly_yield_gain": o["monthly_yield_gain"],
                "payback_days": o["payback_days"],
                "partial": o["partial"],
                "efficiency": o["efficiency"],
            }
            for o in chosen
        ],
        "collection_count": len(chosen),
        "total_monthly_yield_gain": round(total_monthly, 2),
        "total_hourly_yield_gain": round(total_monthly / (30 * 24), 4) if total_monthly else 0,
        "single_best": (
            {
                "id": single_best["id"],
                "name": single_best["name"],
                "boost": single_best["boost"],
                "cost_upx": single_best["cost_upx"],
                "monthly_yield_gain": single_best["monthly_yield_gain"],
                "payback_days": single_best["payback_days"],
            }
            if single_best else None
        ),
        "combo_beats_single": combo_better,
        "all_options_count": len(options),
        "viable_count": len(viable),
        "note": (
            "Maximizes monthly UPX yield gain within budget (exact search for ≤14 options). "
            "Partial = fewer UPX listings than the gap requires (estimate only). "
            "Near-complete collection listings are often heavily marked up — check payback days before buying."
        ),
    }


def build_options_from_almost(almost: list[dict], listings_by_id: dict,
                              annual_rate: float = 0.1225) -> list[dict]:
    """
    almost: list of collection entries from analyze_collections()["almost"]
    listings_by_id: {coll_id: [listing dicts from forsale_finder]}
    """
    options = []
    for coll in almost:
        cid = coll.get("id")
        listings = listings_by_id.get(cid) or listings_by_id.get(str(cid)) or []
        opt = score_collection_option(coll, listings, annual_rate)
        if opt:
            options.append(opt)
    options.sort(key=lambda o: -o["efficiency"])
    return options
