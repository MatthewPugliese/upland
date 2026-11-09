"""
UplandScope — Collection Tracker

Shows which collections a player contributes to, how many properties are
missing from each, and highlights collections that are 1-2 properties away
from completion.

Reuses all parsing/matching logic from collection_optimizer.py.
"""

from collection_optimizer import (
    load_collections,
    load_user_properties,
    parse_collection_requirement,
    find_eligible_properties,
    RARITY_NAMES,
)


def analyze_collections(user_props: list, all_collections: list) -> dict:
    """
    For each collection, count how many of the user's properties qualify.
    Returns structured analysis bucketed by completion status.
    """
    completable = []
    almost = []       # gap 1–2
    contributing = [] # gap > 2 but owns at least 1
    skipped = 0

    for coll in all_collections:
        parsed = parse_collection_requirement(coll)
        req_type = parsed["type"]

        if req_type in ("unparsed", "curated"):
            skipped += 1
            continue

        required = coll.get("amount", parsed.get("amount", 0))
        if required <= 0:
            continue

        eligible = find_eligible_properties(user_props, parsed, coll.get("cityId"))
        owned_count = min(len(eligible), required)
        gap = required - owned_count

        if owned_count == 0:
            continue

        owned_props = eligible[:required]
        rarity_level = coll.get("rarityLevel", 1)
        entry = {
            "id": coll["id"],
            "name": coll["name"],
            "rarity": RARITY_NAMES.get(rarity_level, "Standard"),
            "rarity_level": rarity_level,
            "boost": coll.get("yieldBoost", 1.0),
            "reward": coll.get("oneTimeReward", 0),
            "required": required,
            "owned": owned_count,
            "gap": gap,
            "pct": round(owned_count / required * 100),
            "owned_props": owned_props,
            "owned_mint_sum": round(sum(p.get("mintPrice") or 0 for p in owned_props)),
            "requirements_text": coll.get("requirements", "").rstrip("."),
            "req_type": req_type,
            "parsed_req": parsed,
            "city_id": coll.get("cityId"),
        }

        if gap == 0:
            completable.append(entry)
        elif gap <= 2:
            almost.append(entry)
        else:
            contributing.append(entry)

    # Sort each bucket: almost by gap asc then boost desc; others by owned desc
    almost.sort(key=lambda x: (x["gap"], -x["boost"]))
    contributing.sort(key=lambda x: (-x["pct"], -x["boost"]))
    completable.sort(key=lambda x: -x["boost"])

    return {
        "completable": completable,
        "almost": almost,
        "contributing": contributing,
        "total_contributing": len(completable) + len(almost) + len(contributing),
        "skipped": skipped,
    }


def apply_tracker_header(result: dict, props: list, annual_rate: float,
                         profile: dict | None = None, income: dict | None = None) -> dict:
    """Fill the summary cards.

    Property count and collections that are on come from the public profile.
    Monthly UPX comes from the last earnings collection when that payout is known.
    Otherwise the summary stays on the mint-price estimate.
    """
    total_mint_all = round(sum(p.get("mintPrice") or 0 for p in props))
    result["total_mint_all"] = total_mint_all
    result["annual_rate"] = annual_rate
    result["annual_rate_pct"] = round(annual_rate * 100, 2)

    if profile and profile.get("property_ids"):
        result["total_properties"] = len(profile["property_ids"])
        result["collections_on"] = len(profile.get("collections") or [])
        active = set()
        for coll in profile.get("collections") or []:
            try:
                active.add(int(coll["id"]))
            except (TypeError, ValueError, KeyError):
                continue
        for bucket in ("completable", "almost", "contributing"):
            for entry in result.get(bucket) or []:
                try:
                    entry["on"] = int(entry["id"]) in active
                except (TypeError, ValueError):
                    entry["on"] = False
    else:
        result["total_properties"] = len(props)
        result["collections_on"] = None

    if income and income.get("monthly"):
        result["current_monthly_yield"] = income["monthly"]
        result["current_hourly_yield"] = income["hourly"]
        result["yield_source"] = "collection"
        result["income_as_of"] = income.get("as_of") or ""
        result["income_payout"] = income.get("payout")
    else:
        monthly = round(total_mint_all * annual_rate / 12)
        result["current_monthly_yield"] = monthly
        result["current_hourly_yield"] = round(monthly / (30 * 24), 2)
        result["yield_source"] = "mint"
        result["income_as_of"] = ""
        result["income_payout"] = None
    return result
