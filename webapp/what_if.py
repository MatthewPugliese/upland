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
from known_builds import KNOWN_BUILDS
from structure_fitter import STRUCTURES, structures_that_fit

_OPTIMIZER_CACHE = Path(__file__).resolve().parent.parent / "optimizer" / "cache"
_OWNER_CACHE = CACHE_DIR / "pugs08_blockchain_cache.json"
_RESIDENT_CATS = ("essential", "entertainment", "public", "transportation")
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
    for gap in KNOWN_BUILDS:
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


def _with_health(categories: list[dict]) -> list[dict]:
    colored = []
    for row in categories:
        row = dict(row)
        pct = row.get("pct_of_target") or 0
        row["health"] = "green" if pct >= 80 else ("yellow" if pct >= 40 else "red")
        colored.append(row)
    return colored


def apply_known_builds(hood_name: str, score: dict | None) -> dict | None:
    """Count builds the structures cache has not recorded yet.

    The score page and the what-if table then start from the same numbers.
    """
    if not score:
        return score
    structs = get_cached_structures(hood_name) or {}
    props = get_cached_props(hood_name) or []
    pending = pending_builds(hood_name, props, structs)
    if not pending:
        return score
    folded = fold_pending(score, pending)
    adjusted = dict(score)
    for key in (
        "total_lu", "total_su", "su_per_lu", "office_count",
        "employment_buildings", "props_with_structs", "density_pct",
    ):
        if key in folded:
            adjusted[key] = folded[key]
    categories = _with_health(folded.get("categories") or [])
    adjusted["categories"] = categories
    adjusted["gaps"] = sorted(categories, key=lambda row: row["pct_of_target"])[:3]
    inventory = [dict(row) for row in score.get("inventory") or []]
    by_name = {row.get("name"): row for row in inventory}
    for gap in pending:
        name = gap["structure"]
        info = STRUCTURES.get(name) or {}
        current = by_name.get(name)
        if current:
            current["count"] = (current.get("count") or 0) + 1
            continue
        try:
            from building_images import get_image_url
            image = get_image_url(name)
        except ImportError:
            image = ""
        inventory.append({
            "name": name,
            "count": 1,
            "type": info.get("type", "unknown"),
            "su": info.get("su", 0) or 0,
            "su_cat": _CAT_LABELS.get(info.get("su_cat"), ""),
            "lu": info.get("living_units", 0) or 0,
            "image_url": image,
        })
    inventory.sort(key=lambda row: -(row.get("count") or 0))
    adjusted["inventory"] = inventory
    adjusted["cache_gaps"] = pending
    return adjusted


def fold_pending(score: dict, pending: list[dict]) -> dict:
    """Count known builds in the baseline the what-if table starts from."""
    total_props = score.get("total_props") or 0
    for gap in pending:
        folded = project_score(score, gap["structure"], 0)["after"]
        folded["total_props"] = total_props
        score = folded
    return score


def fold_plan(score: dict, counts: dict, earlier: list[dict]) -> tuple[dict, str]:
    """Count earlier rows in the build order before the row on screen."""
    if not earlier:
        return score, ""
    total_props = score.get("total_props") or 0
    for row in earlier:
        name = row["structure"]
        folded = project_score(score, name, counts.get(name, 0))["after"]
        folded["total_props"] = total_props
        score = folded
        counts[name] = counts.get(name, 0) + 1
    built = ", ".join(f"{row['structure']} on {row['address']}" for row in earlier)
    if len(earlier) == 1:
        return score, f"Counts the earlier build in the order: {built}."
    return score, f"Counts the {len(earlier)} earlier builds in the order: {built}."


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


def _catalog_cat(label: str) -> str | None:
    text = (label or "").strip().lower()
    if text.startswith("public"):
        return "public"
    if text in _RESIDENT_CATS:
        return text
    return None


def catalog_conflicts() -> dict[str, str]:
    """Resident SU values the lot-fit database and the structure catalog do not share.

    Offices are left out. The catalog's office units are commerce, and this
    score treats them as zero Resident SU on purpose.
    """
    cached = getattr(catalog_conflicts, "_cache", None)
    if cached is not None:
        return cached
    found = {}
    path = _OPTIMIZER_CACHE / "buildable_structures.json"
    if path.exists():
        try:
            rows = json.loads(path.read_text()).get("structures") or []
        except (OSError, json.JSONDecodeError):
            rows = []
        by_name = {row.get("name"): row for row in rows}
        for name, info in STRUCTURES.items():
            cat = info.get("su_cat")
            if cat not in _RESIDENT_CATS:
                continue
            row = by_name.get(name)
            if not row or row.get("units") is None:
                continue
            units = float(row["units"])
            catalog_cat = _catalog_cat(row.get("type") or "")
            same_units = abs(units - float(info.get("su") or 0)) < 0.01
            if same_units and catalog_cat == cat:
                continue
            found[name] = (
                f"catalog says {units:g} {row.get('type')}, "
                f"not {float(info.get('su') or 0):g} {_CAT_LABELS.get(cat, cat)}"
            )
    catalog_conflicts._cache = found
    return found


def _spark_brief(name: str) -> dict | None:
    try:
        from spark_estimator import get_spark_cost
    except ImportError:
        return None
    cost = get_spark_cost(name)
    if not cost or cost.get("spark_hours_at_min") is None:
        return None
    return {"hours": cost["spark_hours_at_min"], "estimated": bool(cost.get("estimated"))}


def _pick_label(row: dict) -> str:
    kind = "new type" if row["new_type"] else f"already {row['existing']}"
    spark = ""
    if row["spark_hours"] is not None:
        est = " est" if row["spark_estimated"] else ""
        spark = f" · {row['spark_hours']:g} spark hrs{est}"
    if row.get("catalog"):
        spark += f" · {row['catalog']}"
    if row.get("close_fit"):
        spark += " · within 0.5 of the minimum width"
    return (
        f"{row['structure']} on {row['address']} "
        f"({row['up2']:g} UP², {row['width']:g}^) · "
        f"{row['category_label']} {row['before_pct']}% → {row['after_pct']}% · "
        f"+{row['su']:g} SU · {kind}{spark}"
    )


def best_builds(score: dict, lots: list[dict], counts: dict, limit: int = 8) -> dict:
    """A build order for empty lots you own. Each row uses a different lot.

    The shortest category is filled first. A structure the catalog agrees
    with is chosen over a disputed one in the same category. The percentages
    stack, so a later row starts from the earlier builds. Houses and offices
    are left out. They do not raise a short category.
    """
    categories = list(score.get("categories") or [])
    mine = [lot for lot in lots if lot.get("is_mine")]
    if not categories or not score.get("total_lu"):
        return {"picks": [], "note": "No neighborhood score to compare against."}
    worst = min(categories, key=lambda row: row["pct_of_target"])
    if not mine:
        return {
            "worst": {"key": worst["key"], "label": worst["label"], "pct_of_target": worst["pct_of_target"]},
            "picks": [],
            "note": "No empty lots of yours have a cached size.",
        }

    lu = score["total_lu"]
    conflicts = catalog_conflicts()
    running = {}
    for row in categories:
        running[row["key"]] = dict(row)
        running[row["key"]]["variety_names"] = list(row.get("variety_names") or [])
    used_lots: set[str] = set()
    used_names: set[str] = set()
    picks = []
    skipped: list[dict] = []

    def candidates(cat_key: str | None, agreed_only: bool) -> list[dict]:
        found = []
        for lot in mine:
            if lot["prop_id"] in used_lots:
                continue
            width = lot.get("width") or 0
            depth = lot.get("depth") or 0
            for struct in structures_that_fit(lot.get("up2") or 0, width, depth):
                name = struct["name"]
                struct_cat = struct.get("su_cat")
                su = struct.get("su") or 0
                if name in used_names or not su:
                    continue
                if cat_key is not None and struct_cat != cat_key:
                    continue
                before = running.get(struct_cat)
                if before is None:
                    continue
                catalog = conflicts.get(name, "")
                if agreed_only and catalog:
                    continue
                after_su = before["su"] + su
                target = before["target"]
                after_pct = min(100, round((after_su / lu) / target * 100)) if target else 0
                spark = _spark_brief(name) or {}
                found.append({
                    "prop_id": lot["prop_id"],
                    "address": lot["address"],
                    "up2": lot.get("up2"),
                    "width": width,
                    "structure": name,
                    "su": su,
                    "category": struct_cat,
                    "category_label": before["label"],
                    "before_pct": before["pct_of_target"],
                    "after_pct": after_pct,
                    "pct_gain": after_pct - before["pct_of_target"],
                    "new_type": counts.get(name, 0) == 0,
                    "existing": counts.get(name, 0),
                    "spark_hours": spark.get("hours"),
                    "spark_estimated": spark.get("estimated"),
                    "catalog": catalog,
                    "_waste": (lot.get("up2") or 0) - (struct.get("min_up2") or 0),
                    "_tight": 0 if width - float(struct.get("min_width") or 0) >= 0.5 else 1,
                    "close_fit": width - float(struct.get("min_width") or 0) < 0.5,
                })
        return found

    def take(pool: list[dict]) -> None:
        def sort_key(row: dict):
            spark_sort = row["spark_hours"] if row["spark_hours"] is not None else 10**9
            return (
                -row["pct_gain"],
                0 if row["new_type"] else 1,
                spark_sort,
                row["_tight"],
                row["_waste"],
                row["address"],
                row["structure"],
            )
        roomy = [row for row in pool if not row["close_fit"]]
        chosen = min(roomy or pool, key=sort_key)
        if chosen["close_fit"]:
            return
        cat = running[chosen["category"]]
        cat["su"] = cat["su"] + chosen["su"]
        cat["pct_of_target"] = chosen["after_pct"]
        names = set(cat["variety_names"])
        names.add(chosen["structure"])
        cat["variety_names"] = sorted(names)
        used_lots.add(chosen["prop_id"])
        used_names.add(chosen["structure"])
        picks.append(chosen)

    for cat in sorted(categories, key=lambda row: (row["pct_of_target"], row["key"])):
        if len(picks) >= limit:
            break
        agreed = candidates(cat["key"], agreed_only=True)
        if any(not row["close_fit"] for row in agreed):
            take(agreed)
            continue
        if agreed:
            skipped.append(min(agreed, key=lambda row: (row["_waste"], row["address"], row["structure"])))
            continue
        disputed = candidates(cat["key"], agreed_only=False)
        if disputed:
            sample = min(disputed, key=lambda row: (row["_waste"], row["address"], row["structure"]))
            skipped.append(sample)
    while len(picks) < limit:
        pool = [
            row for row in candidates(None, agreed_only=True)
            if not row["close_fit"] and row["su"] >= 3
        ]
        if not pool:
            break
        take(pool)
    for step, row in enumerate(picks):
        row.pop("_waste", None)
        row.pop("_tight", None)
        row["step"] = step
        row["label"] = _pick_label(row)

    outcome = []
    if picks:
        ending, _ignored = fold_plan(score, dict(counts), picks)
        end_by = {row["key"]: row for row in ending["categories"]}
        for row in categories:
            after = end_by[row["key"]]
            if after["su"] == row["su"]:
                continue
            outcome.append({
                "key": row["key"],
                "label": row["label"],
                "before_pct": row["pct_of_target"],
                "after_pct": after["pct_of_target"],
            })

    if not picks and any(row.get("close_fit") for row in skipped):
        note = "Agreed buildings fit, but only within 0.5 UP of the minimum width, so they are left out."
    elif not picks:
        note = "No agreed service structure fits an empty lot of yours."
    elif picks[0]["category"] != worst["key"]:
        note = (
            f"{worst['label']} is the shortest category at {worst['pct_of_target']}% of target. "
        )
        blocked = next((row for row in skipped if row["category"] == worst["key"]), None)
        if blocked and blocked.get("catalog"):
            note += (
                f"{blocked['structure']} would move it, but {blocked['catalog']}, "
                "so it is left out of this order. "
            )
        else:
            note += f"Nothing that adds {worst['label']} SU fits an empty lot of yours. "
        note += f"The order starts with {picks[0]['structure']} on {picks[0]['address']}."
    else:
        top = picks[0]
        note = (
            f"{worst['label']} is the shortest category at {worst['pct_of_target']}% of target. "
            f"{top['structure']} on {top['address']} is the build that moves it "
            f"({top['before_pct']}% → {top['after_pct']}%)."
        )
        bigger = next((row for row in picks[1:] if row["pct_gain"] > top["pct_gain"]), None)
        if bigger:
            note += (
                f" {bigger['structure']} on {bigger['address']} gains more "
                f"({bigger['pct_gain']} points) in {bigger['category_label']}, "
                "which is already closer to target."
            )
    if len(picks) > 1:
        note += " Each row uses a different lot, and the percentages stack in this order."
    if any(row.get("close_fit") for row in skipped):
        note += " Lots within 0.5 UP of a building's minimum width are left out."
    if outcome:
        moved = ", ".join(
            f"{row['label']} {row['before_pct']}% to {row['after_pct']}%"
            for row in outcome
        )
        note += f" The whole order takes {moved}."
    if any(row["spark_estimated"] for row in picks):
        note += " Spark hours are at the minimum stack. Ones marked est are not from a live construction timer."
    return {
        "worst": {"key": worst["key"], "label": worst["label"], "pct_of_target": worst["pct_of_target"]},
        "picks": picks,
        "outcome": outcome,
        "note": note,
    }


_ACTION_RANK = {"BUILD": 0, "DEMOLISH → BUILD": 1, "KEEP": 2}


def apply_build_order(hood_name: str, rows: list[dict]) -> list[dict]:
    """Put the neighborhood build order on those lots.

    Every other row stays the largest structure that fits the lot.
    """
    if not rows:
        return rows
    payload = build_what_if(hood_name)
    picks = (payload.get("best") or {}).get("picks") or []
    by_id = {str(pick.get("prop_id")): pick for pick in picks}
    for row in rows:
        pick = by_id.get(str(row.get("prop_id") or ""))
        if not pick or row.get("action") != "BUILD" or row.get("current_names"):
            continue
        row["recommended_name"] = pick["structure"]
        row["recommended_su"] = pick["su"]
        row["su_cat"] = pick["category"]
        row["su_gain"] = pick["su"]
        row["build_order"] = (pick.get("step") or 0) + 1
        width = row.get("eff_width")
        width_txt = f"{width}^ × " if width is not None else ""
        row["desc"] = (
            f"{row.get('up2')} UP² ({width_txt}{row.get('depth_up')}^) | "
            f"{pick['structure']} ({pick['su']} {pick.get('category_label') or pick['category']} SU) "
            f"[build order {row['build_order']}]"
        )
    try:
        from spark_estimator import enrich_rows
        enrich_rows(rows)
    except Exception:
        pass
    rows.sort(key=lambda row: (
        0 if row.get("is_mine") else 1,
        row.get("build_order") or 99,
        _ACTION_RANK.get(row.get("action"), 9),
        -(row.get("su_gain") or 0),
    ))
    return rows


def build_what_if(hood_name: str, prop_id: str | None = None, structure: str | None = None,
                  scope: str | None = None, plan_step: str | None = None) -> dict:
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
    counts = _counts(structs)
    for gap in pending:
        counts[gap["structure"]] = counts.get(gap["structure"], 0) + 1
    score = fold_pending(compute_score(hood_name, structs, props), pending)
    payload["best"] = best_builds(score, mine, counts)
    if not shown and scope == "mine":
        payload["note"] = "No empty lots of yours have a cached size."
        return payload
    if not prop_id:
        return payload

    lot = next((item for item in all_lots if item["prop_id"] == str(prop_id)), None)
    if lot is None:
        payload["error"] = "That property is not an empty lot with a cached size."
        return payload

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

    plan_note = ""
    picks = payload["best"].get("picks") or []
    if plan_step not in (None, ""):
        try:
            step = int(plan_step)
        except (TypeError, ValueError):
            step = -1
        if 0 <= step < len(picks):
            chosen = picks[step]
            if chosen["prop_id"] == str(prop_id) and chosen["structure"] == structure:
                score, plan_note = fold_plan(score, counts, picks[:step])
    payload["result"] = project_score(score, structure, counts.get(structure, 0))
    payload["result"]["address"] = lot["address"]
    if plan_note:
        payload["result"]["plan_note"] = plan_note
    return payload
