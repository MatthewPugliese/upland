#!/usr/bin/env python3
"""
Dongan Hills Zone Optimization Map

Generates an interactive HTML map showing Pugs08's optimization zones for
Dongan Hills, Staten Island. Each zone is color-coded with recommended
building assignments shown in property popups.

Usage:
    python3 dongan_hills_zone_map.py
    python3 dongan_hills_zone_map.py --output-dir ~/Desktop

Output:
    Dongan_Hills_Zones.html — Interactive zone map (open in browser)
"""

import argparse
import json
import sys
import time
import concurrent.futures
from pathlib import Path
from collections import defaultdict

import urllib.request

try:
    import folium
    from folium import Popup
except ImportError:
    print("[!] folium not installed. Run: pip install folium")
    sys.exit(1)

try:
    from shapely.geometry import MultiPoint
except ImportError:
    print("[!] shapely not installed. Run: pip install shapely")
    sys.exit(1)

# Import matching and dimension logic
sys.path.insert(0, str(Path(__file__).resolve().parent))
from neighborhood_map import match_to_buildings
from structure_fitter import (
    STRUCTURES, compute_dimensions_up,
    best_service_for_zone, lot_fill_pct, effective_width,
)

# ─────────────────────────────────────────────────────────────────────────────
# Data paths
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / "cache"

PROPS_CACHE = CACHE_DIR / "Dongan_Hills_props_cache.json"
STRUCTURES_CACHE = CACHE_DIR / "Dongan_Hills_structures_cache.json"
PLUTO_CACHE = CACHE_DIR / "Dongan_Hills_pluto_cache.json"
GEOCODE_CACHE = CACHE_DIR / "Dongan_Hills_geocode_cache.json"
BLOCKCHAIN_CACHE = CACHE_DIR / "pugs08_blockchain_cache.json"

# Fallback to old locations if cache dir doesn't exist yet
if not PROPS_CACHE.exists():
    PROPS_CACHE = SCRIPT_DIR / "Dongan_Hills_props_cache.json"
    STRUCTURES_CACHE = SCRIPT_DIR / "Dongan_Hills_structures_cache.json"
    PLUTO_CACHE = SCRIPT_DIR / "Dongan_Hills_pluto_cache.json"
    GEOCODE_CACHE = SCRIPT_DIR / "Dongan_Hills_geocode_cache.json"
    BLOCKCHAIN_CACHE = SCRIPT_DIR / "pugs08_blockchain_cache.json"

# ─────────────────────────────────────────────────────────────────────────────
# Zone definitions
# ─────────────────────────────────────────────────────────────────────────────

ZONE_COLORS = {
    "Zone 1": "#E74C3C",  # Red — Commercial / Liberty Ave
    "Zone 2": "#3498DB",  # Blue — Residential / Dongan Hills Ave
    "Zone 3": "#9B59B6",  # Purple — Public Services / Stobe Ave
    "Zone 4": "#2ECC71",  # Green — Mixed Use / Buel Ave
    "Zone 5": "#F39C12",  # Orange — Industrial / N Railroad + Seaview
    "Zone 6": "#1ABC9C",  # Teal — Green Residential / STEM / Scattered
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

# Street → Zone assignment
STREET_ZONES = {
    "LIBERTY AVE": "Zone 1",
    "DONGAN HILLS AVE": "Zone 2",
    "STOBE AVE": "Zone 3",
    "BUEL AVE": "Zone 4",
    "N RAILROAD AVE": "Zone 5",
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
    "HUSSON ST": "Zone 4",  # Geographically 81m from Buel Ave, 494m from Stobe — reassigned from Zone 3
    "LACONIA AVE": "Zone 6",
}

# ─────────────────────────────────────────────────────────────────────────────
# Manual overrides — special cases the auto-recommender should not change
# ─────────────────────────────────────────────────────────────────────────────

MANUAL_OVERRIDES = {
    # Crown jewel — leave as-is, just note add-ons
    "81295714389886": ("KEEP", "800 UP² | Med Showroom II + Bus Stop — crown jewel; can add Office Complex + Modern Farm Barn"),
    # Pharmacy is a useful mixed-use structure, not worth demolishing
    "81296486138922": ("KEEP", "Pharmacy (East Coast Modular) — service/residential mix, keep"),
    # Funeral Home on narrow lot — keep it, it fits and gives 3 SU
    "81298415518738": ("KEEP", "Funeral Home (3 Pub SU) — fits at 3.2^ wide; more SU than Bus Stop"),
    # Arcade and Bakery on narrow Stobe lots — keep for entertainment variety
    "81296251260674": ("KEEP", "Arcade (3 Ent SU) — keep for entertainment variety"),
    "81296200929029": ("KEEP", "Bakery (3 Ent SU) — keep for entertainment variety"),
    # Apartment + Bus Stop on Liberty — residential anchor, keep
    "81298918835132": ("KEEP", "Apartment Building + Bus Stop — residential anchor on Liberty"),
}

# Zone 6 gets residential-first treatment; all others optimize for SU.
_RESIDENTIAL_ZONES = {"Zone 6", "Zone 2"}

# These structure types count as "trivially low value" — worth demolishing for a better fit.
_LOW_VALUE_TYPES = {"Micro House", "Small Town House"}

# ─────────────────────────────────────────────────────────────────────────────
# Dynamic recommendation engine
# ─────────────────────────────────────────────────────────────────────────────

def auto_recommend(prop_id: str, up2: float, width_up: float, depth_up: float,
                   current_structs: list, zone: str) -> tuple[str, str]:
    """
    Compute the best (action, description) for a property based on its actual
    MapPLUTO dimensions and current structures. Checks MANUAL_OVERRIDES first.
    """
    if prop_id in MANUAL_OVERRIDES:
        return MANUAL_OVERRIDES[prop_id]

    if not up2 or not width_up:
        return ("BUILD", "Unknown size — check Playground before building")

    current_names = [s.get("buildingName", "") for s in current_structs if s.get("buildingName")]
    current_su = sum(STRUCTURES.get(n, {}).get("su", 0) for n in current_names)
    current_lu = sum(STRUCTURES.get(n, {}).get("living_units", 0) for n in current_names)

    # Find best service structure that physically fits
    best_svc = best_service_for_zone(up2, width_up, zone)
    best_su = best_svc["su"] if best_svc else 0
    best_name = best_svc["name"] if best_svc else None

    # For residential-priority zones, also compute best residential
    best_res = None
    if zone in _RESIDENTIAL_ZONES:
        res_fits = [{"name": n, **v} for n, v in STRUCTURES.items()
                    if v["type"] == "residential"
                    and v["min_up2"] <= up2
                    and v.get("min_width", 0) <= width_up]
        if res_fits:
            best_res = max(res_fits, key=lambda x: x["living_units"])

    # Farm / office add-ons (supplementary; don't replace primary recommendation)
    farm_fits = [n for n, v in STRUCTURES.items()
                 if v["type"] == "farm" and v["min_up2"] <= up2 and v.get("min_width", 0) <= width_up]
    best_farm = farm_fits[-1] if farm_fits else None
    office_fits = [n for n, v in STRUCTURES.items()
                   if v["type"] == "office" and v["min_up2"] <= up2 and v.get("min_width", 0) <= width_up]
    best_office = office_fits[-1] if office_fits else None

    # Build the add-on suffix
    # If no service structure fits but an office does, lead with office instead of add-on
    office_only = best_office and not best_name
    addons = []
    if not office_only:
        if zone in ("Zone 5", "Zone 6") and best_farm:
            addons.append(f"{best_farm} (farm)")
        if zone in ("Zone 1", "Zone 5") and best_office:
            # Warn if office fills most of the lot (up2 ratio suggests little room left)
            office_min = STRUCTURES.get(best_office, {}).get("min_up2", 0)
            fill_note = " — fills most of lot, little room for other structures" if office_min > up2 * 0.6 else ""
            addons.append(f"{best_office} (commerce{fill_note})")
    addon_str = " + " + " + ".join(addons) if addons else ""

    # ── Decide action ──────────────────────────────────────────────────────────
    # Nothing built yet
    if not current_names:
        if zone in _RESIDENTIAL_ZONES and best_res and best_su < 5:
            desc = f"{up2} UP² ({width_up}^ × {depth_up}^) | {best_res['name']} ({best_res['living_units']} living units){addon_str}"
        elif best_name:
            desc = f"{up2} UP² ({width_up}^ × {depth_up}^) | {best_name} ({best_su} {best_svc['su_cat']} SU){addon_str}"
        else:
            desc = f"{up2} UP² ({width_up}^ × {depth_up}^) | Bus Stop or Kiosk only (too small/narrow for service structures)"
        return ("BUILD", desc)

    # Something already built — should we demolish?
    # Demolish if: current structures are all low-value AND we can gain meaningful SU
    all_low_value = all(n in _LOW_VALUE_TYPES for n in current_names)
    su_gain = best_su - current_su
    lu_loss = current_lu  # living units we'd lose

    if all_low_value and best_name and su_gain >= 3:
        current_str = ", ".join(current_names)
        desc = (f"{up2} UP² ({width_up}^ × {depth_up}^) | "
                f"Demolish {current_str} → {best_name} ({best_su} {best_svc['su_cat']} SU){addon_str}")
        return ("DEMOLISH → BUILD", desc)

    # Not worth demolishing — but can we add something on an empty lot?
    has_good_service = any(STRUCTURES.get(n, {}).get("su", 0) >= best_su * 0.7 for n in current_names)
    if not has_good_service and best_name and su_gain >= 8:
        current_str = ", ".join(current_names)
        desc = (f"{up2} UP² ({width_up}^ × {depth_up}^) | "
                f"Demolish {current_str} → {best_name} ({best_su} {best_svc['su_cat']} SU) "
                f"(+{su_gain} SU){addon_str}")
        return ("DEMOLISH → BUILD", desc)

    # Keep current — show best achievable context
    current_str = ", ".join(current_names)
    if best_name and best_su > current_su:
        desc = (f"{up2} UP² ({width_up}^ × {depth_up}^) | {current_str} ({current_su} SU) — "
                f"max possible: {best_name} ({best_su} SU){addon_str}")
    else:
        desc = f"{up2} UP² ({width_up}^ × {depth_up}^) | {current_str} — already at or near optimal"
    return ("KEEP", desc)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

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


def load_json(path: Path) -> dict | list:
    with open(path) as f:
        return json.load(f)


def lighten_color(hex_color: str, factor: float = 0.4) -> str:
    """Lighten a hex color by blending toward white."""
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)
    r = int(r + (255 - r) * factor)
    g = int(g + (255 - g) * factor)
    b = int(b + (255 - b) * factor)
    return f"#{r:02x}{g:02x}{b:02x}"


def darken_color(hex_color: str, factor: float = 0.3) -> str:
    """Darken a hex color."""
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)
    r = int(r * (1 - factor))
    g = int(g * (1 - factor))
    b = int(b * (1 - factor))
    return f"#{r:02x}{g:02x}{b:02x}"

# ─────────────────────────────────────────────────────────────────────────────
# Popup HTML
# ─────────────────────────────────────────────────────────────────────────────

def popup_html(prop: dict, structures: list, is_mine: bool,
               zone: str, recommendation: tuple | None,
               dims: dict | None = None) -> str:
    """Build popup HTML for a property."""
    zone_color = ZONE_COLORS.get(zone, "#999")
    zone_name = ZONE_NAMES.get(zone, zone)
    address = prop.get("address", "N/A")
    mint_price = prop.get("mintPrice", "N/A")

    badge = ""
    if is_mine:
        badge = (
            f'<span style="background:#D4A017;color:white;padding:1px 8px;'
            f'border-radius:10px;font-size:11px;margin-left:6px">YOURS</span>'
        )

    zone_badge = (
        f'<span style="background:{zone_color};color:white;padding:1px 8px;'
        f'border-radius:10px;font-size:11px;margin-left:6px">{zone}</span>'
    )

    # Current structures
    if structures:
        struct_lines = []
        for s in structures:
            name = s.get("buildingName", "?")
            btype = s.get("buildingType", "")
            cstatus = s.get("constructionStatus", "")
            label = name
            if cstatus and cstatus != "completed":
                label += f" <i>({cstatus})</i>"
            if btype:
                label += f" <span style='color:#999;font-size:10px'>({btype})</span>"
            struct_lines.append(label)
        struct_html = "<br>".join(struct_lines)
    else:
        struct_html = '<span style="color:#bbb">None (empty lot)</span>'

    # Recommendation
    rec_html = ""
    if recommendation and is_mine:
        action, desc = recommendation
        if action == "KEEP":
            rec_color = "#27AE60"
            icon = "✓"
        elif action.startswith("DEMOLISH"):
            rec_color = "#E74C3C"
            icon = "⚠"
        else:
            rec_color = "#F39C12"
            icon = "★"
        rec_html = (
            f'<div style="margin-top:8px;padding:6px 8px;background:{rec_color}22;'
            f'border-left:3px solid {rec_color};border-radius:3px">'
            f'<span style="font-weight:bold;color:{rec_color}">{icon} {action}</span><br>'
            f'<span style="font-size:12px">{desc}</span>'
            f'</div>'
        )

    # Size row — show UP2, width × depth, and flag narrow lots
    size_html = ""
    if dims:
        w = dims["width_up"]
        d = dims["depth_up"]
        up2 = dims["up2"]
        fill = dims.get("fill_pct", 100)
        eff_w = dims.get("eff_width", w)
        if eff_w < 4:
            width_tag = f' <span style="background:#E74C3C;color:white;padding:1px 5px;border-radius:8px;font-size:10px">VERY NARROW</span>'
        elif eff_w < 6:
            width_tag = f' <span style="background:#F39C12;color:white;padding:1px 5px;border-radius:8px;font-size:10px">NARROW</span>'
        else:
            width_tag = ""
        shape_note = f' <span style="color:#aaa;font-size:10px">({fill}% rect)</span>' if fill < 80 else ""
        size_html = (
            f'<tr><td style="color:#666;padding:3px 10px 3px 0;white-space:nowrap;vertical-align:top">Size</td>'
            f'<td style="padding:3px 0">{up2} UP² &nbsp;<span style="color:#888;font-size:11px">'
            f'({w}^ × {d}^, eff {eff_w}^)</span>{width_tag}{shape_note}</td></tr>'
        )

    return (
        f'<div style="font-family:Arial,sans-serif;font-size:13px;min-width:260px;max-width:340px">'
        f'<b style="font-size:14px">{address}</b>{badge}'
        f'<div style="margin-top:4px">{zone_badge}'
        f'<span style="color:#888;font-size:11px;margin-left:6px">{zone_name}</span></div>'
        f'<table style="border-collapse:collapse;margin-top:8px;width:100%">'
        f'<tr><td style="color:#666;padding:3px 10px 3px 0;white-space:nowrap;vertical-align:top">Mint Price</td>'
        f'<td style="padding:3px 0">{mint_price} UPX</td></tr>'
        f'{size_html}'
        f'<tr><td style="color:#666;padding:3px 10px 3px 0;white-space:nowrap;vertical-align:top">Structures</td>'
        f'<td style="padding:3px 0">{struct_html}</td></tr>'
        f'</table>'
        f'{rec_html}'
        f'<div style="margin-top:6px;color:#aaa;font-size:10px">ID: {prop.get("id","")}</div>'
        f'</div>'
    )

# ─────────────────────────────────────────────────────────────────────────────
# Zone boundary polygon (convex hull of all properties in zone)
# ─────────────────────────────────────────────────────────────────────────────

def compute_zone_hulls(zone_coords: dict) -> dict:
    """
    Given {zone: [(lon, lat), ...]} of all property centroids,
    return {zone: [(lon, lat), ...]} convex hull polygons.
    """
    hulls = {}
    for zone, points in zone_coords.items():
        if len(points) < 3:
            continue
        mp = MultiPoint(points)
        hull = mp.convex_hull
        if hull.geom_type == "Polygon":
            # Buffer slightly for visual padding
            buffered = hull.buffer(0.0004)
            if buffered.geom_type == "Polygon":
                hulls[zone] = list(buffered.exterior.coords)
            else:
                hulls[zone] = list(hull.exterior.coords)
    return hulls

# ─────────────────────────────────────────────────────────────────────────────
# Upland API dimension fetch
# ─────────────────────────────────────────────────────────────────────────────

_API_DIMS_CACHE = CACHE_DIR / "Dongan_Hills_api_dims_cache.json"
_API_DIMS_TTL = 7 * 24 * 3600  # 7 days in seconds


def fetch_api_dims(props: list, user_ids: set) -> dict:
    """
    Fetch lot dimensions for user-owned properties from the Upland API.

    Returns a dict keyed by uppercased/stripped address:
        { "101 LIBERTY AVE": {"up2": int, "width_up": float, "depth_up": float,
                               "fill_pct": int, "eff_width": float}, ... }

    Results are cached in CACHE_DIR with a 7-day TTL.
    """
    # Load from cache if still fresh
    if _API_DIMS_CACHE.exists():
        try:
            cached = json.loads(_API_DIMS_CACHE.read_text())
            age = time.time() - cached.get("_ts", 0)
            if age < _API_DIMS_TTL:
                data = {k: v for k, v in cached.items() if k != "_ts"}
                print(f"[+] Loaded API dims from cache ({len(data)} properties, "
                      f"{int(age/3600)}h old)")
                return data
        except Exception:
            pass  # stale or corrupt — re-fetch

    my_props = [p for p in props if str(p["id"]) in user_ids]
    total = len(my_props)
    print(f"[*] Fetching lot dimensions from Upland API ({total} properties)...")

    results: dict = {}
    fetched_count = [0]

    def _fetch_one(prop: dict) -> tuple[str, dict | None]:
        prop_id = prop["id"]
        url = f"https://api.upland.me/properties/{prop_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            up2 = data["area"]
            coords = json.loads(data["boundaries"])["coordinates"][0]
            w, d = compute_dimensions_up(coords)
            eff_w = effective_width(coords)
            fill = round(lot_fill_pct(coords) * 100)
            key = prop.get("address", "").upper().strip()
            return key, {
                "up2": up2,
                "width_up": round(w, 1),
                "depth_up": round(d, 1),
                "fill_pct": fill,
                "eff_width": eff_w,
            }
        except Exception as exc:
            addr = prop.get("address", str(prop_id))
            print(f"    [!] Failed {addr}: {exc}")
            return prop.get("address", "").upper().strip(), None

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(_fetch_one, p): p for p in my_props}
        for future in concurrent.futures.as_completed(futures):
            key, dims = future.result()
            if dims is not None:
                results[key] = dims
            fetched_count[0] += 1
            if fetched_count[0] % 10 == 0 or fetched_count[0] == total:
                print(f"[+] Got dimensions for {fetched_count[0]}/{total}")

    # Persist to cache with timestamp
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_payload = dict(results)
    cache_payload["_ts"] = time.time()
    _API_DIMS_CACHE.write_text(json.dumps(cache_payload, indent=2))
    print(f"[+] API dims cached → {_API_DIMS_CACHE}")

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Main map generation
# ─────────────────────────────────────────────────────────────────────────────

def generate_zone_map(output_path: Path) -> None:
    print("[*] Loading cached data...")

    # Load all data
    props = load_json(PROPS_CACHE)
    structures = load_json(STRUCTURES_CACHE)
    buildings = load_json(PLUTO_CACHE)
    blockchain = load_json(BLOCKCHAIN_CACHE)

    geocode_map = {}
    if GEOCODE_CACHE.exists():
        geocode_map = load_json(GEOCODE_CACHE)

    # User's property IDs
    user_ids = {str(pid) for pid in blockchain.get("owned", [])}
    my_props_in_hood = [p for p in props if str(p["id"]) in user_ids]

    print(f"[+] {len(props)} total properties")
    print(f"[+] {len(my_props_in_hood)} owned by pugs08")
    print(f"[+] {sum(1 for v in structures.values() if v)} with structures")

    # Fetch lot dimensions from the Upland API (keyed by uppercased address)
    api_dims = fetch_api_dims(props, user_ids)

    def _get_dims(prop: dict) -> dict | None:
        addr = prop.get("address", "").upper().strip()
        return api_dims.get(addr)

    # Match properties to building footprints
    print("[*] Matching properties to building outlines...")
    matched, unmatched = match_to_buildings(props, buildings)

    # Assign zones to all properties
    prop_zones = {}
    for p in props:
        pid = str(p["id"])
        zone = get_zone(p.get("address", ""))
        prop_zones[pid] = zone

    # Compute centroids for zone hulls (user properties only)
    zone_centroids = defaultdict(list)
    for pid, info in matched.items():
        if str(info["prop"].get("id", "")) not in user_ids:
            continue
        coords = info["coords"]
        cx = sum(pt[0] for pt in coords) / len(coords)
        cy = sum(pt[1] for pt in coords) / len(coords)
        zone = prop_zones.get(str(info["prop"]["id"]), "Zone 6")
        zone_centroids[zone].append((cx, cy))

    # Also add geocoded unmatched user props to zone centroids
    for p in unmatched:
        if str(p["id"]) not in user_ids:
            continue
        key = p["address"].upper().strip()
        coords = geocode_map.get(key)
        if coords:
            zone = prop_zones.get(str(p["id"]), "Zone 6")
            zone_centroids[zone].append((coords[1], coords[0]))  # lon, lat

    # Compute zone hull polygons
    print("[*] Computing zone boundaries...")
    zone_hulls = compute_zone_hulls(zone_centroids)

    # ── Build the map ─────────────────────────────────────────────────────────
    print("[*] Rendering map...")

    # Center on Dongan Hills
    all_lats, all_lons = [], []
    for info in matched.values():
        for pt in info["coords"]:
            all_lons.append(pt[0])
            all_lats.append(pt[1])
    center_lat = sum(all_lats) / len(all_lats) if all_lats else 40.588
    center_lon = sum(all_lons) / len(all_lons) if all_lons else -74.098

    m = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=15,
        tiles="CartoDB positron",
    )

    # ── Layer 1: Zone boundary overlays ───────────────────────────────────────
    zone_layer = folium.FeatureGroup(name="Zone Boundaries", show=True)
    for zone, hull_coords in zone_hulls.items():
        color = ZONE_COLORS.get(zone, "#999")
        latlon = [[pt[1], pt[0]] for pt in hull_coords]
        folium.Polygon(
            locations=latlon,
            color=darken_color(color, 0.2),
            weight=2.5,
            fill=True,
            fill_color=color,
            fill_opacity=0.08,
            dash_array="8 4",
            tooltip=(
                f"<b>{zone}: {ZONE_NAMES.get(zone, '')}</b><br>"
                f"<i>{ZONE_DESCRIPTIONS.get(zone, '')}</i>"
            ),
        ).add_to(zone_layer)

        # Zone label at centroid
        pts = [(pt[1], pt[0]) for pt in hull_coords]
        label_lat = sum(p[0] for p in pts) / len(pts)
        label_lon = sum(p[1] for p in pts) / len(pts)
        folium.Marker(
            location=[label_lat, label_lon],
            icon=folium.DivIcon(
                html=(
                    f'<div style="font-size:11px;font-weight:bold;color:{darken_color(color, 0.1)};'
                    f'text-shadow:1px 1px 2px white,-1px -1px 2px white,1px -1px 2px white,-1px 1px 2px white;'
                    f'white-space:nowrap;pointer-events:none">'
                    f'{ZONE_NAMES.get(zone, zone)}</div>'
                ),
                icon_size=(200, 20),
                icon_anchor=(100, 10),
            ),
        ).add_to(zone_layer)

    zone_layer.add_to(m)

    # ── Layer 2: All properties ───────────────────────────────────────────────
    props_layer = folium.FeatureGroup(name="Properties", show=True)

    for pid, info in matched.items():
        prop = info["prop"]
        prop_id = str(prop.get("id", ""))
        is_mine = prop_id in user_ids
        zone = prop_zones.get(prop_id, "Zone 6")
        zone_color = ZONE_COLORS.get(zone, "#999")
        structs = structures.get(prop_id, [])
        dims = _get_dims(prop)

        # Compute recommendation dynamically from actual dimensions
        if is_mine:
            d = dims or {}
            rec = auto_recommend(
                prop_id,
                d.get("up2"), d.get("eff_width", d.get("width_up")), d.get("depth_up"),
                structs, zone,
            )
        else:
            rec = None

        coords_latlon = [[pt[1], pt[0]] for pt in info["coords"]]

        if is_mine:
            fill_color = zone_color
            border_color = darken_color(zone_color, 0.3)
            fill_opacity = 0.7
            weight = 2.5
        else:
            fill_color = "#C0C0C0"  # gray for non-owned
            border_color = "#A0A0A0"
            fill_opacity = 0.3
            weight = 0.8

        struct_names = ", ".join(s["buildingName"] for s in structs if s.get("buildingName"))
        tooltip = prop.get("address", "")
        if is_mine:
            tooltip += f" [{zone}]"
        if struct_names:
            tooltip += f" — {struct_names}"
        if rec and is_mine:
            tooltip += f" | {rec[0]}"

        folium.Polygon(
            locations=coords_latlon,
            color=border_color,
            weight=weight,
            fill=True,
            fill_color=fill_color,
            fill_opacity=fill_opacity,
            popup=Popup(popup_html(prop, structs, is_mine, zone, rec, dims), max_width=380),
            tooltip=tooltip,
        ).add_to(props_layer)

        # Structure/recommendation indicator dot
        if is_mine:
            cx = sum(c[0] for c in coords_latlon) / len(coords_latlon)
            cy = sum(c[1] for c in coords_latlon) / len(coords_latlon)
            if rec and rec[0].startswith("DEMOLISH"):
                dot_color = "#E74C3C"
                dot_radius = 4
            elif rec and rec[0] == "BUILD":
                dot_color = "#F39C12"
                dot_radius = 3
            elif structs:
                dot_color = "white"
                dot_radius = 2.5
            else:
                dot_color = zone_color
                dot_radius = 2
            folium.CircleMarker(
                location=[cx, cy],
                radius=dot_radius,
                color="white",
                weight=1,
                fill=True,
                fill_color=dot_color,
                fill_opacity=0.95,
                tooltip=struct_names or "Empty",
            ).add_to(props_layer)

    # Unmatched properties as circle markers
    for prop in unmatched:
        prop_id = str(prop["id"])
        key = prop["address"].upper().strip()
        coords = geocode_map.get(key)
        if not coords:
            continue
        lat, lon = coords[0], coords[1]
        is_mine = prop_id in user_ids
        zone = prop_zones.get(prop_id, "Zone 6")
        zone_color = ZONE_COLORS.get(zone, "#999")
        structs = structures.get(prop_id, [])
        dims = _get_dims(prop)
        if is_mine:
            d = dims or {}
            rec = auto_recommend(
                prop_id,
                d.get("up2"), d.get("eff_width", d.get("width_up")), d.get("depth_up"),
                structs, zone,
            )
        else:
            rec = None

        if is_mine:
            color = zone_color
            radius = 6
        else:
            color = "#C0C0C0"
            radius = 4

        folium.CircleMarker(
            location=[lat, lon],
            radius=radius,
            color=color,
            weight=2 if is_mine else 1,
            fill=True,
            fill_color=color,
            fill_opacity=0.75 if is_mine else 0.3,
            popup=Popup(popup_html(prop, structs, is_mine, zone, rec, dims), max_width=380),
            tooltip=f"{prop.get('address','')} [{zone}]" if is_mine else prop.get("address", ""),
        ).add_to(props_layer)

    props_layer.add_to(m)

    # ── Layer control ─────────────────────────────────────────────────────────
    folium.LayerControl(collapsed=False).add_to(m)

    # ── Title bar ─────────────────────────────────────────────────────────────
    title_html = (
        '<div style="position:fixed;top:10px;left:50%;transform:translateX(-50%);'
        'background:white;padding:12px 24px;border-radius:10px;'
        'box-shadow:0 2px 12px rgba(0,0,0,.25);z-index:1000;font-family:Arial,sans-serif">'
        '<b style="font-size:17px">Dongan Hills — Zone Optimization Plan</b>'
        f'<span style="color:#666;margin-left:14px;font-size:13px">'
        f'{len(props)} properties • {len(my_props_in_hood)} owned by pugs08</span>'
        '</div>'
    )
    m.get_root().html.add_child(folium.Element(title_html))

    # ── Legend ─────────────────────────────────────────────────────────────────
    legend_items = ""
    for zone_key in ["Zone 1", "Zone 2", "Zone 3", "Zone 4", "Zone 5", "Zone 6"]:
        color = ZONE_COLORS[zone_key]
        name = ZONE_NAMES[zone_key]
        legend_items += (
            f'<div style="margin:5px 0;display:flex;align-items:center">'
            f'<span style="display:inline-block;width:16px;height:16px;'
            f'background:{color};border-radius:3px;margin-right:8px;flex-shrink:0"></span>'
            f'<span style="font-size:12px"><b>{zone_key}</b>: {name}</span>'
            f'</div>'
        )

    indicator_items = (
        '<div style="margin-top:10px;padding-top:8px;border-top:1px solid #eee">'
        '<div style="margin:4px 0"><span style="color:#F39C12;font-size:14px">●</span> '
        '<span style="font-size:11px">Recommended build</span></div>'
        '<div style="margin:4px 0"><span style="color:#E74C3C;font-size:14px">●</span> '
        '<span style="font-size:11px">Demolish & rebuild</span></div>'
        '<div style="margin:4px 0"><span style="color:white;text-shadow:0 0 2px #333;font-size:14px">●</span> '
        '<span style="font-size:11px">Keep existing</span></div>'
        '<div style="margin:4px 0"><span style="color:#C0C0C0;font-size:14px">■</span> '
        '<span style="font-size:11px">Not your property</span></div>'
        '</div>'
    )

    legend_html = (
        '<div style="position:fixed;bottom:30px;right:12px;background:white;padding:14px 18px;'
        'border-radius:10px;box-shadow:0 2px 10px rgba(0,0,0,.25);z-index:1000;'
        'font-family:Arial,sans-serif;max-width:320px">'
        '<b style="display:block;margin-bottom:8px;font-size:14px">Optimization Zones</b>'
        f'{legend_items}'
        f'{indicator_items}'
        '</div>'
    )
    m.get_root().html.add_child(folium.Element(legend_html))

    # ── Stats panel ───────────────────────────────────────────────────────────
    zone_counts = defaultdict(int)
    zone_empty = defaultdict(int)
    for p in my_props_in_hood:
        pid = str(p["id"])
        z = prop_zones.get(pid, "Zone 6")
        zone_counts[z] += 1
        if not structures.get(pid):
            zone_empty[z] += 1

    stats_rows = ""
    for zk in ["Zone 1", "Zone 2", "Zone 3", "Zone 4", "Zone 5", "Zone 6"]:
        c = ZONE_COLORS[zk]
        total = zone_counts.get(zk, 0)
        empty = zone_empty.get(zk, 0)
        stats_rows += (
            f'<tr>'
            f'<td style="padding:2px 6px"><span style="color:{c}">■</span> {zk}</td>'
            f'<td style="padding:2px 6px;text-align:right">{total}</td>'
            f'<td style="padding:2px 6px;text-align:right;color:#999">{empty} empty</td>'
            f'</tr>'
        )

    stats_html = (
        '<div style="position:fixed;bottom:30px;left:12px;background:white;padding:14px 18px;'
        'border-radius:10px;box-shadow:0 2px 10px rgba(0,0,0,.25);z-index:1000;'
        'font-family:Arial,sans-serif">'
        '<b style="display:block;margin-bottom:6px;font-size:13px">Your Properties by Zone</b>'
        f'<table style="font-size:12px;border-collapse:collapse">{stats_rows}</table>'
        f'<div style="margin-top:6px;font-size:11px;color:#888">'
        f'{len(my_props_in_hood)} total • {sum(zone_empty.values())} empty lots</div>'
        '</div>'
    )
    m.get_root().html.add_child(folium.Element(stats_html))

    # Save
    m.save(str(output_path))
    print(f"\n[+] Zone map saved → {output_path}")
    print(f"    Open in browser to explore zones and click properties for recommendations.")


# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Dongan Hills zone optimization map")
    parser.add_argument("--output-dir", default=".", help="Output directory (default: .)")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "Dongan_Hills_Zones.html"

    generate_zone_map(output_path)


if __name__ == "__main__":
    main()
