"""
UplandScope — Neighborhood Score Calculator

Computes Resident Score metrics from the structures cache:
  - Living Units (LU) by building type
  - Service Units (SU) by category (essential, entertainment, public, transportation)
  - SU/LU ratios per category (the key Resident Score inputs)
  - Variety: distinct structure types per category
  - Density: % of properties with at least one building
  - "Biggest gaps": which categories need work most

Data source: Dongan_Hills_structures_cache.json (or any neighborhood's cache).
No live API calls needed — all computed from what we already have cached.
"""

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / "cache" / "neighborhoods"

sys.path.insert(0, str(SCRIPT_DIR.parent / "optimizer"))
from structure_fitter import STRUCTURES
from building_images import get_image_url

# ── SU category display names ──────────────────────────────────────────────
CATEGORY_LABELS = {
    "essential":     "Essential",
    "entertainment": "Entertainment",
    "public":        "Public Services",
    "transportation":"Transportation",
    "employment":    "Employment",
}

# Healthy target ratios (SU per LU) based on Upland docs/community knowledge
# These are approximate — 0.10+ per category is generally considered healthy
SU_TARGETS = {
    "essential":     0.10,
    "entertainment": 0.10,
    "public":        0.10,
    "transportation":0.05,
}

# Buildings not in structure_fitter that we recognize from the API
_EXTRA_BUILDINGS = {
    "Family Home":                      {"type": "residential", "living_units": 2, "su": 0},
    "East Coast Modular Apartments: Pharmacy": {"type": "service", "living_units": 0, "su": 5, "su_cat": "public"},
    "Medium Showroom I":                {"type": "commerce", "living_units": 0, "su": 0},
    "Medium Showroom II":               {"type": "commerce", "living_units": 0, "su": 0},
    "Large Showroom I":                 {"type": "commerce", "living_units": 0, "su": 0},
    "Speedway Structure - Medium":      {"type": "special", "living_units": 0, "su": 0},
}


def _lookup(name: str) -> dict:
    if name in STRUCTURES:
        return STRUCTURES[name]
    if name in _EXTRA_BUILDINGS:
        return _EXTRA_BUILDINGS[name]
    # Partial match for modular/variant names
    for key, info in STRUCTURES.items():
        if key.lower() in name.lower() or name.lower() in key.lower():
            return info
    return {}


def _safe_name(hood_name: str) -> str:
    return "".join(c if c.isalnum() or c in " -_" else "_" for c in hood_name).strip().replace(" ", "_")


def get_cached_structures(hood_name: str) -> dict | None:
    """Load structures cache if it exists. Returns None if not cached."""
    path = CACHE_DIR / f"{_safe_name(hood_name)}_structures_cache.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return None


def get_cached_props(hood_name: str) -> list | None:
    """Load property list cache if it exists."""
    path = CACHE_DIR / f"{_safe_name(hood_name)}_props_cache.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return None


def compute_score(hood_name: str, structs: dict, props: list = None) -> dict:
    """
    Compute Resident Score metrics for a neighborhood.

    Args:
        hood_name: Display name of the neighborhood
        structs: {prop_id: [building_dicts]} from structures cache
        props: Full property list (for density calc), optional

    Returns structured score dict.
    """
    addr_by_id = {str(p.get("id", "")): p.get("address", "") for p in (props or [])}

    total_lu = 0
    su_by_cat = {}
    variety_by_cat = {}
    bldg_counts = {}
    employment_count = 0
    other_buildings = []
    props_with_structs = 0
    total_residents = 0
    empty_residential = []
    has_residents_data = False

    for pid, buildings in structs.items():
        if buildings:
            props_with_structs += 1
        for b in buildings:
            name = b.get("buildingName", "")
            if not name:
                continue
            bldg_counts[name] = bldg_counts.get(name, 0) + 1
            info = _lookup(name)
            if not info:
                other_buildings.append(name)
                continue

            lu = info.get("living_units", 0)
            su = info.get("su", 0)
            cat = info.get("su_cat")
            btype = info.get("type", "")

            total_lu += lu

            if "residents" in b:
                has_residents_data = True
                residents = b.get("residents") or 0
                total_residents += residents
                if btype == "residential" and b.get("constructionStatus") == "completed" and residents == 0:
                    empty_residential.append({
                        "prop_id": pid,
                        "address": addr_by_id.get(pid, ""),
                        "buildingName": name,
                    })

            if btype in ("factory",) or cat == "employment":
                employment_count += 1

            if su and cat and cat != "employment":
                su_by_cat[cat] = su_by_cat.get(cat, 0) + su
                variety_by_cat.setdefault(cat, set()).add(name)

    total_su = sum(su_by_cat.values())
    total_props = len(props) if props else len(structs)
    density_pct = round(props_with_structs / total_props * 100) if total_props else 0

    # Per-category metrics
    categories = []
    for cat in ["essential", "entertainment", "public", "transportation"]:
        su = su_by_cat.get(cat, 0)
        ratio = su / total_lu if total_lu > 0 else 0
        target = SU_TARGETS.get(cat, 0.10)
        pct_of_target = min(100, round(ratio / target * 100)) if target else 0
        varieties = variety_by_cat.get(cat, set())
        categories.append({
            "key": cat,
            "label": CATEGORY_LABELS[cat],
            "su": su,
            "ratio": round(ratio, 3),
            "target": target,
            "pct_of_target": pct_of_target,
            "variety": len(varieties),
            "variety_names": sorted(varieties),
            "health": "green" if pct_of_target >= 80 else ("yellow" if pct_of_target >= 40 else "red"),
        })

    # Biggest gaps: categories furthest from target
    gaps = sorted(categories, key=lambda x: x["pct_of_target"])[:3]

    # Building inventory sorted by count desc, with per-type info and image URL
    inventory = []
    for name, count in sorted(bldg_counts.items(), key=lambda x: -x[1]):
        info = _lookup(name) or {}
        inventory.append({
            "name": name,
            "count": count,
            "type": info.get("type", "unknown"),
            "su": info.get("su", 0) or 0,
            "su_cat": CATEGORY_LABELS.get(info.get("su_cat", ""), ""),
            "lu": info.get("living_units", 0) or 0,
            "image_url": get_image_url(name),
        })

    return {
        "neighborhood": hood_name,
        "total_lu": total_lu,
        "total_su": total_su,
        "su_per_lu": round(total_su / total_lu, 3) if total_lu else 0,
        "total_props": total_props,
        "props_with_structs": props_with_structs,
        "density_pct": density_pct,
        "employment_buildings": employment_count,
        "has_residents_data": has_residents_data,
        "total_residents": total_residents,
        "empty_residential": empty_residential,
        "categories": categories,
        "gaps": gaps,
        "inventory": inventory,
        "other_buildings": list(set(other_buildings)),
    }


def get_neighborhood_score(hood_name: str) -> dict | None:
    """
    Load cached data and compute score for a neighborhood.
    Returns None if no structures cache exists.
    """
    structs = get_cached_structures(hood_name)
    if structs is None:
        return None
    props = get_cached_props(hood_name)
    return compute_score(hood_name, structs, props)


def list_cached_neighborhoods() -> list[str]:
    """Return neighborhoods that have a structures cache."""
    results = []
    for path in CACHE_DIR.glob("*_structures_cache.json"):
        name = path.stem.replace("_structures_cache", "").replace("_", " ").title()
        results.append(name)
    return sorted(results)
