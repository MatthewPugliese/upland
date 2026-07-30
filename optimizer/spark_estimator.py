"""
Spark Hours Estimator

Estimates construction spark cost for recommended builds using live Upland API
fields observed on in-progress and completed buildings:

  totalSparksRequired  — construction "work units" (only present while building)
  minStackedSparks     — minimum stack to start/maintain construction
  maxStackedSparks     — max stack (theoretical fastest finish)
  stepSparks           — construction class (10 ≈ residential, 100 ≈ service/special)

Spark hours are reported as time-to-complete at minimum stack:

    spark_hours_at_min = totalSparksRequired / minStackedSparks / 3600

When totalSparksRequired is unknown (completed buildings / never observed under
construction), we estimate it from minStackedSparks × a ratio calibrated on the
known sample set. Estimates are flagged so the UI can show them as approximate.

Usage:
    from spark_estimator import enrich_rows, summarize_queue, get_spark_cost

    rows = enrich_rows(report_rows)          # mutates/adds spark fields per row
    summary = summarize_queue(rows, mine_only=True)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / "cache"
PROFILE_PATH = CACHE_DIR / "spark_profiles.json"

# ── Known totalSparksRequired from live construction objects (2026-07) ──────
# Keys are stripped building names. Values are exact totals from the public API.
_KNOWN_TOTALS: dict[str, float] = {
    "Micro House": 1_500_000,
    "Small Town House": 2_850_000,
    "Ranch House": 3_300_000,
    "Town House": 7_800_000,
    "Urban Residence": 9_000_000,
    "Dollar Store": 14_400_000,
    "Apartment Building": 17_400_000,
    "Junior College": 54_900_000,
    "Large Factory I": 66_000_000,
}

# ── Stack / step profiles from live API `details` (completed + processing) ──
# min/max/step are stable per structure type; total is filled from _KNOWN_TOTALS
# when available, otherwise estimated.
_PROFILES: dict[str, dict] = {
    "Micro House":           {"step": 10,  "min": 50,   "max": 72000},
    "Small Town House":      {"step": 10,  "min": 100,  "max": 30000},
    "Ranch House":           {"step": 10,  "min": 100,  "max": 30000},
    "Town House":            {"step": 10,  "min": 200,  "max": 60000},
    "Contemporary House":    {"step": 10,  "min": 250,  "max": 45000},
    "Luxury Ranch House":    {"step": 10,  "min": 300,  "max": 75000},
    "Luxury Modern House":   {"step": 10,  "min": 300,  "max": 75000},
    "Apartment Building":    {"step": 10,  "min": 400,  "max": 90000},
    "Glass Tower":           {"step": 100, "min": 390,  "max": 51000},
    "Urban Residence":       {"step": 100, "min": 250,  "max": 45000},
    "Family Home":           {"step": 100, "min": 100,  "max": 39000},
    "Small Factory I":       {"step": 10,  "min": 500,  "max": 66000},
    "Micro Factory I":       {"step": 100, "min": 300,  "max": 66000},
    "Large Factory I":       {"step": 100, "min": 500,  "max": 120000},
    "Bus Stop":              {"step": 100, "min": 70,   "max": 45000},
    "Information Kiosk":     {"step": 100, "min": 70,   "max": 36000},  # est. same class as Bus Stop
    "Sausage Stand":         {"step": 100, "min": 140,  "max": 36000},  # est. same class as Hot Dog kiosk
    "Donut Stand":           {"step": 100, "min": 140,  "max": 36000},
    "Kiosk - Hot Dog":       {"step": 100, "min": 140,  "max": 36000},
    "Kiosk - Hamburger":     {"step": 100, "min": 140,  "max": 39000},
    "Coffee Stand":          {"step": 100, "min": 160,  "max": 39000},  # est. between kiosk and bakery
    "Bodega":                {"step": 100, "min": 190,  "max": 39000},
    "Barn & Nobles":         {"step": 100, "min": 190,  "max": 39000},  # est. ~Bodega tier
    "Antique Store":         {"step": 100, "min": 190,  "max": 39000},
    "Toy Store":             {"step": 100, "min": 190,  "max": 39000},
    "Bike Shop":             {"step": 100, "min": 190,  "max": 39000},
    "Bakery":                {"step": 100, "min": 160,  "max": 39000},
    "Arcade":                {"step": 100, "min": 190,  "max": 39000},
    "Funeral Home":          {"step": 100, "min": 210,  "max": 42000},  # est. ~Dry Cleaner
    "Dry Cleaner":           {"step": 100, "min": 210,  "max": 42000},
    "Pizzeria":              {"step": 100, "min": 210,  "max": 42000},
    "Fast Food Joint":       {"step": 100, "min": 240,  "max": 42000},
    "Dollar Store":          {"step": 100, "min": 270,  "max": 45000},
    "Wheel Alignment Center":{"step": 100, "min": 270,  "max": 45000},  # est. ~Dollar Store tier
    "Pool Hall":             {"step": 100, "min": 270,  "max": 45000},
    "Art Gallery":           {"step": 100, "min": 240,  "max": 42000},
    "Day Care Center":       {"step": 100, "min": 290,  "max": 45000},
    "Fire Station":          {"step": 100, "min": 330,  "max": 48000},
    "Police Detention Center":{"step": 100, "min": 400, "max": 60000},  # est. large public
    "Auto Repair Shop":      {"step": 100, "min": 300,  "max": 48000},
    "Car Rental":            {"step": 100, "min": 300,  "max": 48000},
    "Try Harder Gym":        {"step": 100, "min": 330,  "max": 51000},
    "Junior College":        {"step": 100, "min": 500,  "max": 81000},
    "Micro Showroom I":      {"step": 100, "min": 300,  "max": 60000},
    "Small Showroom II":     {"step": 100, "min": 500,  "max": 57000},
    "Medium Showroom I":     {"step": 100, "min": 500,  "max": 72000},
    "Medium Showroom II":    {"step": 100, "min": 500,  "max": 57000},
    "Large Showroom I":      {"step": 100, "min": 500,  "max": 78000},
    "Speedway Structure - Medium": {"step": 100, "min": 1000, "max": 300000},
    "East Coast Modular Apartments: Pharmacy": {
        "step": 100, "min": 240, "max": 42000,
    },
}

# total/min ratios calibrated on known construction samples
#   residential (step 10): ~30k–43.5k  → use 36_000
#   small/mid service (step 100, min < 400): ~36k–53k → use 45_000
#   large service/factory (step 100, min ≥ 400): ~110k–132k → use 110_000
_RATIO_RESIDENTIAL = 36_000
_RATIO_SERVICE_SMALL = 45_000
_RATIO_SERVICE_LARGE = 110_000

# Structures with min_up2 ≥ this and no better profile use "large" ratio
_LARGE_MIN_UP2 = 30

# Flag bottom-quartile SU-per-hour builds as spark-heavy (among rows with both
# SU gain and hours). Adaptive so sparse high-SU queues still surface duds.

try:
    from structure_fitter import STRUCTURES
except ImportError:
    STRUCTURES = {}


def _norm(name: Optional[str]) -> str:
    return (name or "").strip()


def _estimate_ratio(step: int, min_stack: int, min_up2: Optional[float] = None) -> float:
    if step <= 10:
        return _RATIO_RESIDENTIAL
    if min_stack >= 400 or (min_up2 is not None and min_up2 >= _LARGE_MIN_UP2):
        return _RATIO_SERVICE_LARGE
    return _RATIO_SERVICE_SMALL


def _fallback_profile(name: str) -> dict:
    """Rough profile for structures we've never sampled live."""
    info = STRUCTURES.get(name) or {}
    min_up2 = info.get("min_up2") or 12
    btype = info.get("type") or "service"
    # min stack scales loosely with size; calibrated to known pairs
    if btype == "residential":
        step = 10
        min_stack = max(50, int(min_up2 * 8))
        max_stack = max(30000, min_stack * 150)
    else:
        step = 100
        min_stack = max(70, int(min_up2 * 12))
        max_stack = max(36000, min_stack * 150)
    return {"step": step, "min": min_stack, "max": max_stack, "min_up2": min_up2}


def get_spark_cost(structure_name: Optional[str]) -> Optional[dict]:
    """
    Return spark cost estimate for a structure type.

    Keys:
        name, step_sparks, min_stacked, max_stacked,
        total_sparks, spark_hours_at_min, spark_hours_at_max,
        estimated (bool), source ("measured" | "ratio" | "size_fallback")
    """
    name = _norm(structure_name)
    if not name:
        return None

    profile = _PROFILES.get(name)
    source = "measured"
    if profile:
        step = profile["step"]
        min_stack = profile["min"]
        max_stack = profile["max"]
        min_up2 = (STRUCTURES.get(name) or {}).get("min_up2")
    else:
        fb = _fallback_profile(name)
        step, min_stack, max_stack = fb["step"], fb["min"], fb["max"]
        min_up2 = fb.get("min_up2")
        source = "size_fallback"

    if name in _KNOWN_TOTALS:
        total = float(_KNOWN_TOTALS[name])
        estimated = False
        source = "measured"
    else:
        ratio = _estimate_ratio(step, min_stack, min_up2)
        total = float(min_stack * ratio)
        estimated = True
        if source == "measured":
            source = "ratio"

    hours_min = round(total / min_stack / 3600, 2) if min_stack else None
    hours_max = round(total / max_stack / 3600, 3) if max_stack else None

    return {
        "name": name,
        "step_sparks": step,
        "min_stacked": min_stack,
        "max_stacked": max_stack,
        "total_sparks": int(total),
        "spark_hours_at_min": hours_min,
        "spark_hours_at_max": hours_max,
        "estimated": estimated,
        "source": source,
    }


def enrich_rows(rows: list[dict]) -> list[dict]:
    """
    Add spark cost fields to each recommendation row (in place).

    New keys on each actionable row (BUILD / DEMOLISH → BUILD):
        spark_hours, spark_hours_max, total_sparks, min_stacked,
        spark_estimated, su_per_spark_hour, spark_heavy
    KEEP rows get spark fields as None.
    """
    costs: list[Optional[dict]] = []
    for r in rows:
        action = r.get("action") or ""
        name = r.get("recommended_name")
        if action in ("BUILD", "DEMOLISH → BUILD") and name:
            cost = get_spark_cost(name)
        else:
            cost = None
        costs.append(cost)

        if cost:
            hours = cost["spark_hours_at_min"]
            su = r.get("su_gain") or 0
            su_per = round(su / hours, 3) if hours and hours > 0 and su > 0 else None
            r["spark_hours"] = hours
            r["spark_hours_max"] = cost["spark_hours_at_max"]
            r["total_sparks"] = cost["total_sparks"]
            r["min_stacked"] = cost["min_stacked"]
            r["spark_estimated"] = cost["estimated"]
            r["su_per_spark_hour"] = su_per
            r["spark_heavy"] = False  # set below
        else:
            r["spark_hours"] = None
            r["spark_hours_max"] = None
            r["total_sparks"] = None
            r["min_stacked"] = None
            r["spark_estimated"] = None
            r["su_per_spark_hour"] = None
            r["spark_heavy"] = False

    # Flag bottom-quartile efficiency as spark-heavy
    efficiencies = sorted(
        r["su_per_spark_hour"]
        for r in rows
        if r.get("su_per_spark_hour") is not None
    )
    if efficiencies:
        q1 = efficiencies[max(0, len(efficiencies) // 4)]
        for r in rows:
            su_per = r.get("su_per_spark_hour")
            if su_per is not None and su_per <= q1:
                r["spark_heavy"] = True

    return rows


def summarize_queue(rows: list[dict], mine_only: bool = True) -> dict:
    """
    Summarize spark cost for the recommended action queue.

    By default only counts owned properties with BUILD or DEMOLISH → BUILD.
    """
    actionable = []
    for r in rows:
        if mine_only and not r.get("is_mine"):
            continue
        if r.get("action") not in ("BUILD", "DEMOLISH → BUILD"):
            continue
        if r.get("spark_hours") is None:
            continue
        actionable.append(r)

    total_hours = sum(r["spark_hours"] or 0 for r in actionable)
    total_su = sum(r.get("su_gain") or 0 for r in actionable)
    measured = sum(1 for r in actionable if not r.get("spark_estimated"))
    estimated = sum(1 for r in actionable if r.get("spark_estimated"))
    heavy = [r for r in actionable if r.get("spark_heavy")]

    # Best efficiency picks (highest SU per spark hour among those with SU gain)
    ranked = sorted(
        [r for r in actionable if r.get("su_per_spark_hour")],
        key=lambda r: r["su_per_spark_hour"],
        reverse=True,
    )

    return {
        "action_count": len(actionable),
        "total_spark_hours": round(total_hours, 1),
        "total_su_gain": round(total_su, 1),
        "su_per_spark_hour": round(total_su / total_hours, 3) if total_hours else None,
        "measured_count": measured,
        "estimated_count": estimated,
        "spark_heavy_count": len(heavy),
        "best_efficiency": [
            {
                "address": r.get("address"),
                "structure": r.get("recommended_name"),
                "su_gain": r.get("su_gain"),
                "spark_hours": r.get("spark_hours"),
                "su_per_spark_hour": r.get("su_per_spark_hour"),
                "estimated": r.get("spark_estimated"),
            }
            for r in ranked[:5]
        ],
        "worst_efficiency": [
            {
                "address": r.get("address"),
                "structure": r.get("recommended_name"),
                "su_gain": r.get("su_gain"),
                "spark_hours": r.get("spark_hours"),
                "su_per_spark_hour": r.get("su_per_spark_hour"),
                "estimated": r.get("spark_estimated"),
            }
            for r in ranked[-5:][::-1]
        ] if ranked else [],
        "note": (
            "Spark hours = totalSparksRequired / minStackedSparks / 3600 "
            "(time to finish at minimum stack). Stack more sparks to finish faster. "
            "Values marked estimated use a calibrated ratio when exact construction "
            "totals have not been observed live."
        ),
    }


def merge_live_profile(name: str, details: dict, construction: Optional[dict] = None) -> None:
    """
    Update in-memory profiles from a live API building payload.
    Does not persist — call save_profiles() if you want disk cache.
    """
    name = _norm(name)
    if not name:
        return
    step = details.get("stepSparks")
    mn = details.get("minStackedSparks")
    mx = details.get("maxStackedSparks")
    if step is not None and mn is not None:
        _PROFILES[name] = {
            "step": int(step),
            "min": int(mn),
            "max": int(mx or mn * 150),
        }
    if construction and construction.get("totalSparksRequired") is not None:
        try:
            _KNOWN_TOTALS[name] = float(construction["totalSparksRequired"])
        except (TypeError, ValueError):
            pass


def save_profiles(path: Path = PROFILE_PATH) -> None:
    """Persist known totals + profiles for reuse across runs."""
    payload = {
        "totals": _KNOWN_TOTALS,
        "profiles": _PROFILES,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def load_profiles(path: Path = PROFILE_PATH) -> bool:
    """Load persisted profiles if present. Returns True if loaded."""
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    for name, total in (data.get("totals") or {}).items():
        try:
            _KNOWN_TOTALS[_norm(name)] = float(total)
        except (TypeError, ValueError):
            pass
    for name, prof in (data.get("profiles") or {}).items():
        if isinstance(prof, dict) and "min" in prof and "step" in prof:
            _PROFILES[_norm(name)] = {
                "step": int(prof["step"]),
                "min": int(prof["min"]),
                "max": int(prof.get("max") or prof["min"] * 150),
            }
    return True


# Load any on-disk refinements at import time
load_profiles()


if __name__ == "__main__":
    # Quick smoke test against common recommendations
    samples = [
        "Police Detention Center",
        "Wheel Alignment Center",
        "Coffee Stand",
        "Sausage Stand",
        "Information Kiosk",
        "Bus Stop",
        "Town House",
        "Dollar Store",
        "Micro House",
        "Apartment Building",
        "Day Care Center",
    ]
    print(f"{'Structure':30} {'hrs@min':>8} {'SU':>5} {'est?':>5} {'source':>14} {'minStack':>8}")
    for name in samples:
        c = get_spark_cost(name)
        info = STRUCTURES.get(name) or {}
        su = info.get("su") or 0
        print(
            f"{name:30} {c['spark_hours_at_min']:8.2f} {su:5} "
            f"{'yes' if c['estimated'] else 'no':>5} {c['source']:>14} {c['min_stacked']:8}"
        )
