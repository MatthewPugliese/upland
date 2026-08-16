"""
Dongan Hills neighborhood recommendation engine.

Standalone module — no folium/shapely dependency, safe to import from the webapp.
Contains zone definitions, auto_recommend(), and generate_report().
"""
import json
import sys
from pathlib import Path

# Make structure_fitter importable from either the optimizer dir or from the webapp
sys.path.insert(0, str(Path(__file__).resolve().parent))
from structure_fitter import STRUCTURES, best_service_for_zone

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / "cache"

# ─── Zone definitions ─────────────────────────────────────────────────────────

ZONE_COLORS = {
    "Zone 1": "#E74C3C",
    "Zone 2": "#3498DB",
    "Zone 3": "#9B59B6",
    "Zone 4": "#2ECC71",
    "Zone 5": "#F39C12",
    "Zone 6": "#1ABC9C",
}

ZONE_NAMES = {
    "Zone 1": "Liberty Ave — Commercial & Entertainment",
    "Zone 2": "Dongan Hills Ave — Residential Core",
    "Zone 3": "Stobe Ave — Public Services Hub",
    "Zone 4": "Buel Ave — Mixed Residential & Employment",
    "Zone 5": "N Railroad & Seaview — Industrial/Transit",
    "Zone 6": "Naughton & Scattered — Green/STEM Residential",
}

ZONE_DESCRIPTIONS = {
    "Zone 1": "Main Street corridor. High-value entertainment & essential service structures.",
    "Zone 2": "Preserve existing residential. Add essential service variety.",
    "Zone 3": "Public service anchor zone. Court House, Pool, DMV, Day Care.",
    "Zone 4": "Mid-density residential + employment structures.",
    "Zone 5": "Factories, transportation hubs, and employment.",
    "Zone 6": "Residential with heavy STEM/greenery focus. Future nursery site.",
}

STREET_ZONES = {
    "LIBERTY AVE": "Zone 1",
    "DONGAN HILLS AVE": "Zone 2",
    "STOBE AVE": "Zone 3",
    "BUEL AVE": "Zone 4",
    "N RAILROAD AVE": "Zone 5",
    "RAILROAD AVENUE": "Zone 5",
    "SEAVIEW AVE": "Zone 5",
    "NAUGHTON AVE": "Zone 6",
    "VERA ST": "Zone 6",
    "JEFFERSON AVE": "Zone 6",
    "JEFFERSON ST": "Zone 6",
    "SLATER BLVD": "Zone 6",
    "SEAVER AVE": "Zone 6",
    "ZOE ST": "Zone 6",
    "CLETUS ST": "Zone 6",
    "BOUNDARY AVE": "Zone 6",
    "HUSSON ST": "Zone 4",
    "LACONIA AVE": "Zone 6",
}

# ─── Recommendation constants ─────────────────────────────────────────────────

MANUAL_OVERRIDES = {
    "81295714389886": ("KEEP", "800 UP² | Med Showroom II + Bus Stop — crown jewel; can add Office Complex + Modern Farm Barn"),
    "81296486138922": ("KEEP", "Pharmacy (East Coast Modular) — service/residential mix, keep"),
    "81298415518738": ("KEEP", "Funeral Home (3 Pub SU) — fits at 3.2^ wide; more SU than Bus Stop"),
    "81296251260674": ("KEEP", "Arcade (3 Ent SU) — keep for entertainment variety"),
    "81296200929029": ("KEEP", "Bakery (3 Ent SU) — keep for entertainment variety"),
    "81298918835132": ("KEEP", "Apartment Building + Bus Stop — residential anchor on Liberty"),
}

_RESIDENTIAL_ZONES = {"Zone 6", "Zone 2", "residential", "green"}
_LOW_VALUE_TYPES = {"Micro House", "Small Town House"}


def _rule_based_action(prop_id: str, current_structs: list) -> tuple[str, str] | None:
    """
    Return (action, desc) for a portable rule-based override, or None.
    Handles Showrooms (MetaVentures) and unknown/limited structures safely.
    Works for any neighborhood — does not rely on hardcoded property IDs.
    """
    names = [s.get("buildingName", "") for s in current_structs if s.get("buildingName")]
    if not names:
        return None
    for name in names:
        if "Showroom" in name:
            return ("KEEP", f"{name} — MetaVenture property (never demolish)")
    for name in names:
        if name and name not in STRUCTURES:
            return ("KEEP", f"{name} — limited/event structure; not in standard DB")
    return None


def get_zone(address: str) -> str:
    """Assign a zone based on street name."""
    addr = address.upper().strip()
    parts = addr.split(maxsplit=1)
    if len(parts) < 2:
        return "Zone 6"
    street = parts[1]
    for street_key, zone in STREET_ZONES.items():
        if street_key in street:
            return zone
    return "Zone 6"


def compute_lu_balance(structures: dict, user_ids: set = None) -> dict:
    """
    Compute living unit vs service unit balance for user-owned properties.

    structures: {prop_id: [{buildingName, ...}, ...]}  (neighborhood structures cache)
    user_ids:   set of prop_id strings to include (None = all props in dict)

    Returns:
        total_lu  — living units currently built
        total_su  — total service units built
        by_cat    — {essential: N, entertainment: N, public: N, employment: N, transportation: N}
        ratios    — {cat: SU/LU, ...}  (0 when total_lu == 0)
        status    — "balanced" | "lu_deficit" | "lu_critical" | "su_deficit"
        message   — human-readable summary line
    """
    total_lu = 0
    by_cat: dict[str, int] = {"essential": 0, "entertainment": 0, "public": 0,
                               "employment": 0, "transportation": 0}
    total_su = 0

    for prop_id, structs_list in structures.items():
        if user_ids is not None and str(prop_id) not in user_ids:
            continue
        for s in structs_list:
            name = s.get("buildingName", "")
            if not name:
                continue
            info = STRUCTURES.get(name, {})
            total_lu += info.get("living_units", 0)
            su = info.get("su", 0)
            total_su += su
            cat = info.get("su_cat")
            if cat in by_cat:
                by_cat[cat] += su

    if total_lu == 0:
        ratios = {cat: 0 for cat in by_cat}
        status = "lu_critical" if total_su > 0 else "balanced"
        msg = ("No living units — SU ratios undefined. Build residential structures urgently."
               if total_su > 0 else "No structures built yet.")
    else:
        ratios = {cat: round(v / total_lu, 2) for cat, v in by_cat.items()}
        overall = total_su / total_lu
        if overall > 12:
            status = "lu_deficit"
            msg = (f"{total_su} SU / {total_lu} LU = {overall:.1f}× — over-served. "
                   "Prioritize residential structures.")
        elif total_su > 0 and overall < 2:
            status = "su_deficit"
            msg = (f"{total_su} SU / {total_lu} LU = {overall:.1f}× — under-served. "
                   "Prioritize service structures.")
        else:
            status = "balanced"
            msg = f"{total_su} SU / {total_lu} LU = {overall:.1f}× — balanced."

    return {
        "total_lu": total_lu,
        "total_su": total_su,
        "by_cat": by_cat,
        "ratios": ratios,
        "status": status,
        "message": msg,
    }


def auto_recommend(prop_id: str, up2: float, width_up: float, depth_up: float,
                   current_structs: list, zone: str,
                   neighborhood_counts: dict = None,
                   lu_deficit: bool = False) -> dict:
    """
    Compute the best recommendation for a property.

    neighborhood_counts: {structure_name: count_in_neighborhood}
      When provided, recommends structure types not yet present in the
      neighborhood before repeating types that are already well-covered.

    lu_deficit: when True (SU/LU ratio > 12 for user-owned properties), treat
      ALL zones as residential-eligible and lower the SU threshold for preferring
      residential over service structures to 10 SU (vs the normal 5).

    Returns a dict:
        action           — "BUILD", "DEMOLISH → BUILD", or "KEEP"
        desc             — human-readable string (used in map popups)
        recommended_name — structure to build/keep (str or None)
        recommended_su   — SU of recommended structure
        su_cat           — SU category string or None
        su_gain          — net SU gained by taking action (0 for KEEP)
        current_names    — list of currently built structure names
        current_su       — total SU of current structures
        addons           — list of supplementary structure suggestions
        up2              — lot area in UP²
        width_up         — raw width in UP units
        depth_up         — depth in UP units
    """
    _rule = _rule_based_action(prop_id, current_structs)
    if _rule:
        action, desc = _rule
        current_names = [s.get("buildingName", "") for s in current_structs if s.get("buildingName")]
        current_su = sum(STRUCTURES.get(n, {}).get("su", 0) for n in current_names)
        return {
            "action": action, "desc": desc,
            "recommended_name": None, "recommended_su": current_su,
            "su_cat": None, "su_gain": 0,
            "current_names": current_names, "current_su": current_su,
            "addons": [], "up2": up2, "width_up": width_up, "depth_up": depth_up,
        }

    if prop_id in MANUAL_OVERRIDES:
        action, desc = MANUAL_OVERRIDES[prop_id]
        current_names = [s.get("buildingName", "") for s in current_structs if s.get("buildingName")]
        current_su = sum(STRUCTURES.get(n, {}).get("su", 0) for n in current_names)
        return {
            "action": action,
            "desc": desc,
            "recommended_name": None,
            "recommended_su": current_su,
            "su_cat": None,
            "su_gain": 0,
            "current_names": current_names,
            "current_su": current_su,
            "addons": [],
            "up2": up2,
            "width_up": width_up,
            "depth_up": depth_up,
        }

    if not up2 or not width_up:
        return {
            "action": "BUILD",
            "desc": "Unknown size — check Playground before building",
            "recommended_name": None,
            "recommended_su": 0,
            "su_cat": None,
            "su_gain": 0,
            "current_names": [],
            "current_su": 0,
            "addons": [],
            "up2": up2,
            "width_up": width_up,
            "depth_up": depth_up,
        }

    current_names = [s.get("buildingName", "") for s in current_structs if s.get("buildingName")]
    current_su = sum(STRUCTURES.get(n, {}).get("su", 0) for n in current_names)

    best_svc = best_service_for_zone(up2, width_up, zone, neighborhood_counts, depth_up)
    best_su = best_svc["su"] if best_svc else 0
    best_name = best_svc["name"] if best_svc else None

    # Compact variety note appended to descriptions
    def _variety_tag(name):
        if not neighborhood_counts or not name:
            return ""
        count = neighborhood_counts.get(name, 0)
        if count == 0:
            return " [new type]"
        if count >= 3:
            return f" [{count}× in nbhd]"
        return ""

    # Consider residential on residential zones always; on any zone when LU deficit
    _res_eligible = zone in _RESIDENTIAL_ZONES or lu_deficit
    _res_su_threshold = 10 if lu_deficit else 5  # raise bar for service when LU is low

    best_res = None
    if _res_eligible:
        res_fits = [{"name": n, **v} for n, v in STRUCTURES.items()
                    if v["type"] == "residential"
                    and v["min_up2"] <= up2
                    and v.get("min_width", 0) <= width_up]
        if res_fits:
            best_res = max(res_fits, key=lambda x: x["living_units"])

    farm_fits = [n for n, v in STRUCTURES.items()
                 if v["type"] == "farm" and v["min_up2"] <= up2 and v.get("min_width", 0) <= width_up]
    best_farm = farm_fits[-1] if farm_fits else None
    office_fits = [n for n, v in STRUCTURES.items()
                   if v["type"] == "office" and v["min_up2"] <= up2 and v.get("min_width", 0) <= width_up]
    best_office = office_fits[-1] if office_fits else None

    office_only = best_office and not best_name
    addons = []
    if not office_only:
        if zone in ("Zone 5", "Zone 6") and best_farm:
            addons.append(f"{best_farm} (farm)")
        if zone in ("Zone 1", "Zone 5") and best_office:
            office_min = STRUCTURES.get(best_office, {}).get("min_up2", 0)
            fill_note = " — fills most of lot, little room for other structures" if office_min > up2 * 0.6 else ""
            addons.append(f"{best_office} (commerce{fill_note})")
    addon_str = " + " + " + ".join(addons) if addons else ""

    su_gain = best_su - current_su

    # ── Nothing built yet ────────────────────────────────────────────────────
    if not current_names:
        if _res_eligible and best_res and best_su < _res_su_threshold:
            desc = f"{up2} UP² ({width_up}^ × {depth_up}^) | {best_res['name']} ({best_res['living_units']} living units){addon_str}"
            return {
                "action": "BUILD",
                "desc": desc,
                "recommended_name": best_res["name"],
                "recommended_su": 0,
                "su_cat": None,
                "su_gain": 0,
                "current_names": current_names,
                "current_su": current_su,
                "addons": addons,
                "up2": up2,
                "width_up": width_up,
                "depth_up": depth_up,
            }
        elif best_name:
            desc = f"{up2} UP² ({width_up}^ × {depth_up}^) | {best_name} ({best_su} {best_svc['su_cat']} SU){_variety_tag(best_name)}{addon_str}"
            return {
                "action": "BUILD",
                "desc": desc,
                "recommended_name": best_name,
                "recommended_su": best_su,
                "su_cat": best_svc["su_cat"],
                "su_gain": best_su,
                "current_names": current_names,
                "current_su": current_su,
                "addons": addons,
                "up2": up2,
                "width_up": width_up,
                "depth_up": depth_up,
            }
        else:
            desc = f"{up2} UP² ({width_up}^ × {depth_up}^) | Bus Stop or Kiosk only (too small/narrow for service structures)"
            return {
                "action": "BUILD",
                "desc": desc,
                "recommended_name": "Bus Stop",
                "recommended_su": 0,
                "su_cat": None,
                "su_gain": 0,
                "current_names": current_names,
                "current_su": current_su,
                "addons": addons,
                "up2": up2,
                "width_up": width_up,
                "depth_up": depth_up,
            }

    # ── Something built — should we demolish? ───────────────────────────────
    all_low_value = all(n in _LOW_VALUE_TYPES for n in current_names)
    if all_low_value and best_name and su_gain >= 3:
        current_str = ", ".join(current_names)
        desc = (f"{up2} UP² ({width_up}^ × {depth_up}^) | "
                f"Demolish {current_str} → {best_name} ({best_su} {best_svc['su_cat']} SU){_variety_tag(best_name)}{addon_str}")
        return {
            "action": "DEMOLISH → BUILD",
            "desc": desc,
            "recommended_name": best_name,
            "recommended_su": best_su,
            "su_cat": best_svc["su_cat"],
            "su_gain": su_gain,
            "current_names": current_names,
            "current_su": current_su,
            "addons": addons,
            "up2": up2,
            "width_up": width_up,
            "depth_up": depth_up,
        }

    has_good_service = any(STRUCTURES.get(n, {}).get("su", 0) >= best_su * 0.7 for n in current_names)
    if not has_good_service and best_name and su_gain >= 8:
        current_str = ", ".join(current_names)
        desc = (f"{up2} UP² ({width_up}^ × {depth_up}^) | "
                f"Demolish {current_str} → {best_name} ({best_su} {best_svc['su_cat']} SU)"
                f"{_variety_tag(best_name)} (+{su_gain} SU){addon_str}")
        return {
            "action": "DEMOLISH → BUILD",
            "desc": desc,
            "recommended_name": best_name,
            "recommended_su": best_su,
            "su_cat": best_svc["su_cat"],
            "su_gain": su_gain,
            "current_names": current_names,
            "current_su": current_su,
            "addons": addons,
            "up2": up2,
            "width_up": width_up,
            "depth_up": depth_up,
        }

    # ── Keep current ─────────────────────────────────────────────────────────
    current_str = ", ".join(current_names)
    if best_name and best_su > current_su:
        desc = (f"{up2} UP² ({width_up}^ × {depth_up}^) | {current_str} ({current_su} SU) — "
                f"max possible: {best_name} ({best_su} SU){addon_str}")
    else:
        desc = f"{up2} UP² ({width_up}^ × {depth_up}^) | {current_str} — already at or near optimal"
    return {
        "action": "KEEP",
        "desc": desc,
        "recommended_name": best_name,
        "recommended_su": best_su,
        "su_cat": best_svc["su_cat"] if best_svc else None,
        "su_gain": 0,
        "current_names": current_names,
        "current_su": current_su,
        "addons": addons,
        "up2": up2,
        "width_up": width_up,
        "depth_up": depth_up,
    }


def generate_report(neighborhood: str = "Dongan_Hills") -> dict:
    """
    Load cached data and return recommendations for all properties.

    Returns:
        rows        — list of row dicts (see below), sorted: owned-first,
                      then by action priority, then by su_gain descending
        lu_balance  — output of compute_lu_balance() for user-owned props
        neighborhood_counts — {structure_name: count} across all props

    Each row dict:
        prop_id, address, zone, up2, eff_width, width_up, depth_up,
        is_mine, action, recommended_name, recommended_su, su_cat,
        su_gain, current_names, current_su, addons, desc
    """
    cache_dir = CACHE_DIR
    props_path = cache_dir / f"{neighborhood}_props_cache.json"
    structs_path = cache_dir / f"{neighborhood}_structures_cache.json"
    dims_path = cache_dir / f"{neighborhood}_api_dims_cache.json"
    blockchain_path = cache_dir / "pugs08_blockchain_cache.json"

    if not props_path.exists():
        return []

    props = json.loads(props_path.read_text())
    structures = json.loads(structs_path.read_text()) if structs_path.exists() else {}
    dims_raw = json.loads(dims_path.read_text()) if dims_path.exists() else {}
    api_dims = {k: v for k, v in dims_raw.items() if k != "_ts"}
    blockchain = json.loads(blockchain_path.read_text()) if blockchain_path.exists() else {}
    user_ids = {str(pid) for pid in blockchain.get("owned", [])}

    # Count how many of each structure type exist across the whole neighborhood
    neighborhood_counts: dict[str, int] = {}
    for structs_list in structures.values():
        for s in structs_list:
            name = s.get("buildingName", "")
            if name:
                neighborhood_counts[name] = neighborhood_counts.get(name, 0) + 1

    # LU balance check — scope to user-owned props only
    lu_balance = compute_lu_balance(structures, user_ids=user_ids)
    lu_deficit = lu_balance["status"] in ("lu_deficit", "lu_critical")

    rows = []
    for prop in props:
        prop_id = str(prop.get("id", ""))
        address = prop.get("address", "")
        zone = get_zone(address)
        structs = structures.get(prop_id, [])
        dims = api_dims.get(address.upper().strip())
        is_mine = prop_id in user_ids

        d = dims or {}
        rec = auto_recommend(
            prop_id,
            d.get("up2"),
            d.get("eff_width", d.get("width_up")),
            d.get("depth_up"),
            structs,
            zone,
            neighborhood_counts,
            lu_deficit=lu_deficit and is_mine,  # only nudge residential on owned props
        )

        rows.append({
            "prop_id": prop_id,
            "address": address,
            "zone": zone,
            "up2": d.get("up2"),
            "eff_width": d.get("eff_width"),
            "width_up": d.get("width_up"),
            "depth_up": d.get("depth_up"),
            "is_mine": is_mine,
            "action": rec["action"],
            "recommended_name": rec["recommended_name"],
            "recommended_su": rec["recommended_su"],
            "su_cat": rec["su_cat"],
            "su_gain": rec["su_gain"],
            "current_names": rec["current_names"],
            "current_su": rec["current_su"],
            "addons": rec["addons"],
            "desc": rec["desc"],
        })

    _ACTION_PRIORITY = {"DEMOLISH → BUILD": 0, "BUILD": 1, "KEEP": 2}

    def _sort_key(r):
        return (
            0 if r["is_mine"] else 1,
            _ACTION_PRIORITY.get(r["action"], 9),
            -(r["su_gain"] or 0),
        )

    rows.sort(key=_sort_key)

    # Annotate BUILD/DEMOLISH rows with spark-hour cost + SU-per-hour efficiency
    try:
        from spark_estimator import enrich_rows, summarize_queue
        enrich_rows(rows)
        spark_summary = summarize_queue(rows, mine_only=True)
    except Exception:
        spark_summary = None

    # Plan completion: which owned recommended actions are already done
    plan_progress = compute_plan_progress(rows, mine_only=True)

    # Commerce Score: office inventory + empty lots that could host offices
    commerce = compute_commerce_summary(rows, structures, user_ids)

    return {
        "rows": rows,
        "lu_balance": lu_balance,
        "neighborhood_counts": neighborhood_counts,
        "spark_summary": spark_summary,
        "plan_progress": plan_progress,
        "commerce": commerce,
    }


def compute_plan_progress(rows: list[dict], mine_only: bool = True) -> dict:
    """
    Compare recommended actions vs currently built structures on owned lots.

    A BUILD action is "done" if the recommended structure is already present.
    A DEMOLISH → BUILD is "done" if the recommended structure is present
    (implies the old low-value structure was replaced).
    KEEP rows are not part of the plan queue.

    Returns counts, % complete, and remaining action lists broken down by type.
    """
    actionable = []
    for r in rows:
        if mine_only and not r.get("is_mine"):
            continue
        action = r.get("action") or ""
        if action not in ("BUILD", "DEMOLISH → BUILD"):
            continue
        rec_name = r.get("recommended_name")
        if not rec_name:
            continue
        current = set(r.get("current_names") or [])
        done = rec_name in current
        entry = {
            "address": r.get("address"),
            "action": action,
            "recommended_name": rec_name,
            "su_gain": r.get("su_gain") or 0,
            "spark_hours": r.get("spark_hours"),
            "done": done,
            "current_names": r.get("current_names") or [],
        }
        actionable.append(entry)

    done_list = [a for a in actionable if a["done"]]
    remaining = [a for a in actionable if not a["done"]]
    by_action = {
        "BUILD": sum(1 for a in remaining if a["action"] == "BUILD"),
        "DEMOLISH → BUILD": sum(1 for a in remaining if a["action"] == "DEMOLISH → BUILD"),
    }
    total = len(actionable)
    done_n = len(done_list)
    remaining_su = round(sum(a["su_gain"] for a in remaining), 1)
    remaining_spark = round(
        sum(a["spark_hours"] or 0 for a in remaining), 1
    )

    return {
        "total_actions": total,
        "done": done_n,
        "remaining": len(remaining),
        "pct_complete": round(done_n / total * 100) if total else 100,
        "remaining_by_action": by_action,
        "remaining_su_gain": remaining_su,
        "remaining_spark_hours": remaining_spark if any(a.get("spark_hours") for a in remaining) else None,
        "remaining_actions": remaining[:25],  # cap for API payload
        "done_actions": done_list[:10],
    }


def compute_commerce_summary(rows: list[dict], structures: dict, user_ids: set) -> dict:
    """
    Commerce Score layer: offices don't grant Resident SU but feed Commerce Score.

    - Count office structures on owned properties
    - Flag empty/underused owned lots in commercial/industrial zones that fit an office
    - Surface best office fit per such lot
    """
    from structure_fitter import STRUCTURES, structures_that_fit

    office_names = {
        n for n, info in STRUCTURES.items() if info.get("type") == "office"
    }
    # Zones where offices make sense
    commerce_zones = {"Zone 1", "Zone 5", "commercial", "industrial", "mixed", "Zone 4"}

    owned_offices = []
    office_lots = 0
    for pid in user_ids:
        for b in structures.get(str(pid), []) or []:
            name = b.get("buildingName") or ""
            if name in office_names or "Office" in name:
                owned_offices.append({"prop_id": str(pid), "buildingName": name})
                office_lots += 1

    # Empty owned lots in commerce-friendly zones that can fit an office
    opportunities = []
    for r in rows:
        if not r.get("is_mine"):
            continue
        if r.get("zone") not in commerce_zones:
            continue
        current = r.get("current_names") or []
        # Skip if already has an office or is a protected MetaVenture
        if any(n in office_names or "Office" in n or "Showroom" in n for n in current):
            continue
        up2 = r.get("up2")
        width = r.get("eff_width") or r.get("width_up") or 0
        if not up2 or not width:
            continue
        offices = [
            s for s in structures_that_fit(up2, width, r.get("depth_up") or 0)
            if s.get("type") == "office"
        ]
        if not offices:
            continue
        # Prefer largest office that fits
        best = max(offices, key=lambda s: s.get("min_up2", 0))
        # Only surface if lot is empty or only has low-value residential
        is_empty = len(current) == 0
        only_low = all(n in _LOW_VALUE_TYPES for n in current) if current else False
        if not (is_empty or only_low or r.get("action") in ("BUILD", "DEMOLISH → BUILD")):
            # Still note as optional commerce addon if lot has headroom
            if r.get("action") != "KEEP":
                continue
            # KEEP with headroom — skip unless empty-ish
            if current and not only_low:
                continue
        opportunities.append({
            "address": r.get("address"),
            "zone": r.get("zone"),
            "up2": up2,
            "eff_width": width,
            "best_office": best["name"],
            "current_names": current,
            "is_empty": is_empty,
            "action": r.get("action"),
        })

    # Sort: empty first, then by office size desc
    opportunities.sort(key=lambda o: (0 if o["is_empty"] else 1, -(o.get("up2") or 0)))

    return {
        "office_count": len(owned_offices),
        "office_lots": office_lots,
        "offices": owned_offices[:20],
        "opportunity_count": len(opportunities),
        "opportunities": opportunities[:15],
        "note": (
            "Offices grant 0 Resident SU but feed Commerce Score (office units + bonds). "
            "Place on commercial/industrial lots when service coverage is already healthy."
        ),
    }
