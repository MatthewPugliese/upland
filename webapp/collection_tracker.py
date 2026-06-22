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
            "owned_props": eligible[:required],
            "requirements_text": coll.get("requirements", "").rstrip("."),
            "req_type": req_type,
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
