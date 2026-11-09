"""What-if build simulator.

Answers "if I put this structure on this empty lot, what happens to the
neighborhood score?" from the structures cache, the lot-size cache, and the
structure database. No Upland API calls.

The score is the whole neighborhood, the same numbers as /score. One building
is often a small move against hundreds of living units. The result says so.
"""

from __future__ import annotations

import json
from pathlib import Path

from score_calculator import (
    CACHE_DIR,
    SU_TARGETS,
    _safe_name,
    compute_score,
    get_cached_props,
    get_cached_structures,
)
from structure_fitter import STRUCTURES, structures_that_fit

_OPTIMIZER_CACHE = Path(__file__).resolve().parent.parent / "optimizer" / "cache"
_OWNER_CACHE = CACHE_DIR / "pugs08_blockchain_cache.json"
_RESIDENT_CATS = ("essential", "entertainment", "public", "transportation")
# The structures cache still lists these as empty. They are already built.
_CACHE_GAPS = (
    {"neighborhood": "dongan hills", "address": "114 SEAVIEW AVE", "structure": "Barn & Nobles"},
)
_CAT_LABELS = {
    "essential": "Essential",
    "entertainment": "Entertainment",
    "public": "Public Services",
    "transportation": "Transportation",
    "employment": "Employment",
    "commerce": "Commerce",
    "farming": "Farming",
}


def load_dims(hood_name: str) -> dict | None:
    path = _OPTIMIZER_CACHE / f"{_safe_name(hood_name)}_api_dims_cache.json"
    if not path.exists():
        return None
    with open(path) as handle:
        raw = json.load(handle)
    return {
        key.upper().strip(): value
        for key, value in raw.items()
        if key != "_ts" and isinstance(value, dict)
    }


def load_owned_ids() -> set[str]:
    if not _OWNER_CACHE.exists():
        return set()
    try:
        data = json.loads(_OWNER_CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    return {str(prop_id) for prop_id in data.get("owned") or []}


def _fit_width(dim: dict) -> float:
    """Buildable width. eff_width is what the recommender already uses."""
    eff = dim.get("eff_width")
    if eff:
        return float(eff)
    return float(dim.get("width_up") or 0)


def list_empty_lots(props: list, structs: dict, dims: dict, owned: set[str] | None = None) -> list[dict]:
    """Empty means the structures cache has this id and the building list is empty.

    A missing id is skipped. We don't know whether that lot is empty.
    """
    owned = owned or set()
    lots = []
    for prop in props or []:
        prop_id = str(prop.get("id") or "")
        if prop_id not in structs:
            continue
        if structs.get(prop_id):
            continue
        address = (prop.get("address") or "").strip()
        dim = dims.get(address.upper())
        if not dim:
            continue
        lots.append({
            "prop_id": prop_id,
            "address": address,
            "up2": dim.get("up2"),
            "width": _fit_width(dim),
            "width_up": dim.get("width_up"),
            "depth": dim.get("depth_up") or 0,
            "is_mine": prop_id in owned,
        })
    lots.sort(key=lambda lot: (not lot["is_mine"], lot["address"]))
    return lots


def fit_failure(up2, width, depth, name: str) -> str | None:
    """None when the structure fits. Otherwise a short reason."""
    info = STRUCTURES.get(name)
    if info is None:
        return f"{name} is not in the structure database"
    if up2 is None or info["min_up2"] > up2:
        return f"needs {info['min_up2']} UP², this lot is {up2}"
    if width and info.get("min_width", 0) > width:
        return f"needs {info['min_width']}^ of width, this lot has {width}^"
    if depth and info.get("min_depth", 0) > depth:
        return f"needs {info['min_depth']}^ of depth, this lot has {depth}^"
    return None


def _addr(address: str) -> str:
    return (address or "").upper().strip()


def pending_builds(hood_name: str, props: list, structs: dict) -> list[dict]:
    """Builds we know about that this neighborhood's structures cache has not recorded."""
    ids_by_address = {
        _addr(prop.get("address")): str(prop.get("id") or "")
        for prop in props or []
    }
    hood = (hood_name or "").strip().lower()
    pending = []
    for gap in _CACHE_GAPS:
        if gap["neighborhood"] != hood:
            continue
        prop_id = ids_by_address.get(gap["address"])
        if not prop_id:
            continue
        names = {
            (building.get("buildingName") or "")
            for building in (structs.get(prop_id) or [])
            if isinstance(building, dict)
        }
        if gap["structure"] in names:
            continue
        pending.append({"address": gap["address"], "structure": gap["structure"]})
    return pending


def fold_pending(score: dict, pending: list[dict]) -> dict:
    """Count known builds in the baseline the what-if table starts from."""
    total_props = score.get("total_props") or 0
    for gap in pending:
        folded = project_score(score, gap["structure"], 0)["after"]
        folded["total_props"] = total_props
        score = folded
    return score


def _counts(structs: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for buildings in structs.values():
        for building in buildings or []:
            name = building.get("buildingName") or ""
            if name:
                counts[name] = counts.get(name, 0) + 1
    return counts


def fitting_structures(lot: dict, counts: dict[str, int]) -> list[dict]:
    fits = structures_that_fit(lot.get("up2") or 0, lot.get("width") or 0, lot.get("depth") or 0)
    rows = []
    for info in fits:
        name = info["name"]
        cat = info.get("su_cat")
        rows.append({
            "name": name,
            "su": info.get("su") or 0,
            "lu": info.get("living_units") or 0,
            "type": info.get("type") or "",
            "category": cat,
            "category_label": _CAT_LABELS.get(cat, ""),
            "existing": counts.get(name, 0),
        })
    rows.sort(key=lambda row: (-row["su"], -row["lu"], row["name"]))
    return rows


def _categories(su_by_cat: dict, variety: dict, total_lu: float) -> list[dict]:
    categories = []
    for cat in _RESIDENT_CATS:
        su = su_by_cat.get(cat, 0)
        ratio = (su / total_lu) if total_lu else 0
        target = SU_TARGETS[cat]
        pct = min(100, round(ratio / target * 100)) if target else 0
        names = sorted(variety.get(cat) or [])
        categories.append({
            "key": cat,
            "label": _CAT_LABELS[cat],
            "su": su,
            "ratio": round(ratio, 3),
            "target": target,
            "pct_of_target": pct,
            "variety": len(names),
            "variety_names": names,
        })
    return categories


def project_score(score: dict, name: str, existing: int) -> dict:
    """Neighborhood score after adding one structure on an empty lot."""
    info = STRUCTURES.get(name) or {}
    added_lu = info.get("living_units") or 0
    added_su = info.get("su") or 0
    cat = info.get("su_cat")
    total_lu = (score.get("total_lu") or 0) + added_lu

    su_by = {row["key"]: row["su"] for row in score.get("categories") or []}
    variety = {row["key"]: set(row.get("variety_names") or []) for row in score.get("categories") or []}
    new_type = False
    if added_su and cat in su_by:
        su_by[cat] = su_by[cat] + added_su
        new_type = name not in variety[cat]
        variety[cat].add(name)

    before_cats = score.get("categories") or []
    after_cats = _categories(su_by, variety, total_lu)
    # Recompute the "before" ratios at the same rounding, in case the stored
    # score was built with a different LU than we're about to compare.
    before_su = {row["key"]: row["su"] for row in before_cats}
    before_variety = {row["key"]: set(row.get("variety_names") or []) for row in before_cats}
    before_view = _categories(before_su, before_variety, score.get("total_lu") or 0)

    office = score.get("office_count") or 0
    employment = score.get("employment_buildings") or 0
    office_delta = 0
    employment_delta = 0
    if info.get("type") == "office" or cat == "commerce":
        office_delta = 1
    if info.get("type") == "factory" or cat == "employment":
        employment_delta = 1

    developed = (score.get("props_with_structs") or 0) + 1
    total_props = score.get("total_props") or 0
    after_su_total = sum(row["su"] for row in after_cats)
    before_su_total = sum(row["su"] for row in before_view)

    result = {
        "structure": name,
        "fits": True,
        "adds": {
            "su": added_su,
            "lu": added_lu,
            "category": cat if cat in _RESIDENT_CATS else None,
            "category_label": _CAT_LABELS.get(cat, ""),
            "new_type": new_type,
            "existing": existing,
            "office": office_delta,
            "employment": employment_delta,
        },
        "before": {
            "total_lu": score.get("total_lu") or 0,
            "total_su": before_su_total,
            "su_per_lu": round(before_su_total / score["total_lu"], 3) if score.get("total_lu") else 0,
            "office_count": office,
            "employment_buildings": employment,
            "categories": before_view,
        },
        "after": {
            "total_lu": total_lu,
            "total_su": after_su_total,
            "su_per_lu": round(after_su_total / total_lu, 3) if total_lu else 0,
            "office_count": office + office_delta,
            "employment_buildings": employment + employment_delta,
            "props_with_structs": developed,
            "density_pct": round(developed / total_props * 100) if total_props else 0,
            "categories": after_cats,
        },
    }
    result["summary"] = _summary(result, info)
    return result


def _summary(result: dict, info: dict) -> str:
    name = result["structure"]
    adds = result["adds"]
    before = result["before"]
    after = result["after"]
    cat = info.get("su_cat")
    if adds["su"] and adds["category"]:
        before_cat = next(row for row in before["categories"] if row["key"] == adds["category"])
        after_cat = next(row for row in after["categories"] if row["key"] == adds["category"])
        kind = "a new type" if adds["new_type"] else f"already {adds['existing']} in the neighborhood"
        return (
            f"{name} adds {adds['su']:g} {before_cat['label']} SU ({kind}). "
            f"{before_cat['label']} goes from {before_cat['su']:g} to {after_cat['su']:g} SU, "
            f"{before_cat['pct_of_target']}% of target to {after_cat['pct_of_target']}%."
        )
    if adds["lu"] and not adds["su"]:
        dropped = [
            after_cat["label"]
            for before_cat, after_cat in zip(before["categories"], after["categories"])
            if before_cat["su"] and after_cat["ratio"] < before_cat["ratio"]
        ]
        if dropped:
            which = ", ".join(dropped)
            return (
                f"{name} adds {adds['lu']} living units and no service units. "
                f"The same services are spread across more homes, so {which} SU/LU falls."
            )
        return (
            f"{name} adds {adds['lu']} living units and no service units. "
            "The same services are spread across more homes. "
            "The change is smaller than the 0.001 the score rounds to."
        )
    if info.get("type") == "office" or cat == "commerce":
        return (
            f"{name} does not change Resident SU or living units. "
            f"Commerce offices go from {before['office_count']} to {after['office_count']}."
        )
    if info.get("type") == "factory" or cat == "employment":
        return (
            f"{name} does not change Resident SU or living units. "
            f"Employment buildings go from {before['employment_buildings']} to {after['employment_buildings']}."
        )
    if info.get("type") == "farm" or cat == "farming":
        return (
            f"{name} does not change Resident SU or living units. "
            "Farm scoring is separate from the Resident Score."
        )
    return f"{name} does not change Resident SU or living units."


def build_what_if(hood_name: str, prop_id: str | None = None, structure: str | None = None,
                  scope: str | None = None) -> dict:
    hood_name = (hood_name or "").strip() or "Dongan Hills"
    structs = get_cached_structures(hood_name)
    if structs is None:
        return {"neighborhood": hood_name, "lots": [], "error": "No structures cache for this neighborhood."}
    props = get_cached_props(hood_name) or []
    dims = load_dims(hood_name)
    if not dims:
        return {
            "neighborhood": hood_name,
            "lots": [],
            "note": "Lot sizes are not cached for this neighborhood. Generate an optimization map first.",
        }

    owned = load_owned_ids()
    pending = pending_builds(hood_name, props, structs)
    pending_addresses = {gap["address"] for gap in pending}
    all_lots = [
        lot for lot in list_empty_lots(props, structs, dims, owned)
        if _addr(lot["address"]) not in pending_addresses
    ]
    for lot in all_lots:
        lot["fit_count"] = len(structures_that_fit(
            lot.get("up2") or 0, lot.get("width") or 0, lot.get("depth") or 0,
        ))
    mine = [lot for lot in all_lots if lot["is_mine"]]
    if scope not in ("mine", "all"):
        scope = "mine" if mine else "all"
    shown = mine if scope == "mine" else all_lots
    payload = {
        "neighborhood": hood_name,
        "scope": scope,
        "mine_count": len(mine),
        "all_count": len(all_lots),
        "lots": shown,
        "cache_gaps": pending,
    }
    if not shown and scope == "mine":
        payload["note"] = "No empty lots of yours have a cached size."
        return payload
    if not prop_id:
        return payload

    lot = next((item for item in all_lots if item["prop_id"] == str(prop_id)), None)
    if lot is None:
        payload["error"] = "That property is not an empty lot with a cached size."
        return payload

    counts = _counts(structs)
    for gap in pending:
        counts[gap["structure"]] = counts.get(gap["structure"], 0) + 1
    payload["lot"] = lot
    payload["structures"] = fitting_structures(lot, counts)
    if not structure:
        if not payload["structures"]:
            payload["note"] = (
                f"Nothing in the structure database fits {lot['address']} "
                f"({lot['up2']} UP², {lot['width']}^ wide)."
            )
        return payload

    reason = fit_failure(lot.get("up2"), lot.get("width") or 0, lot.get("depth") or 0, structure)
    if reason:
        payload["result"] = {"structure": structure, "fits": False, "reason": reason}
        return payload

    score = fold_pending(compute_score(hood_name, structs, props), pending)
    payload["result"] = project_score(score, structure, counts.get(structure, 0))
    payload["result"]["address"] = lot["address"]
    return payload
