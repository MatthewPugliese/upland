#!/usr/bin/env python3
"""
Upland Neighborhood Zone Optimization Map

Generalized version — works for any Upland neighborhood.
Automatically assigns zones from OSM street classifications via Overpass API.

Usage:
    python3 zone_map.py "Dongan Hills" --city "Staten Island"
    python3 zone_map.py "Rosebank" --city "Staten Island" --username pugs08
    python3 zone_map.py "Inner Richmond" --city "San Francisco"
    python3 zone_map.py "Lincoln Park" --city "Chicago" --output-dir ~/Desktop

Output:
    output/<NeighborhoodName>_Zones.html  — Interactive zone optimization map
"""

import argparse
import json
import sys
import time
import concurrent.futures
import urllib.request
from pathlib import Path
from collections import defaultdict

try:
    import folium
    from folium import Popup
except ImportError:
    print("[!] folium not installed. Run: pip install folium")
    sys.exit(1)

try:
    from shapely.geometry import MultiPoint, LineString, Polygon as ShapelyPolygon
except ImportError:
    print("[!] shapely not installed. Run: pip install shapely")
    sys.exit(1)

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from neighborhood_map import (
    find_neighborhood,
    get_neighborhood_properties,
    get_upland_property_structures,
    get_user_property_ids,
    get_nyc_pluto_parcels,
    get_overpass_buildings,
    match_to_buildings,
    geocode_props,
    _addr_nodes_to_geocode_map,
    _is_nyc,
    _poly_to_overpass_str,
    _overpass_query,
    _STREET_ABBREV,
    DEFAULT_USERNAME,
    DEFAULT_EOS_ACCOUNT,
)
from structure_fitter import (
    STRUCTURES,
    compute_dimensions_up,
    lot_fill_pct,
    effective_width,
)
from recommender import auto_recommend, compute_lu_balance

# ─────────────────────────────────────────────────────────────────────────────
# Generic zone definitions (OSM-based)
# ─────────────────────────────────────────────────────────────────────────────

ZONE_TYPES = {
    "commercial":  {
        "color": "#E74C3C",
        "name": "Commercial Corridor",
        "desc": "Primary/secondary streets — entertainment & essential services",
    },
    "residential": {
        "color": "#3498DB",
        "name": "Residential",
        "desc": "Housing-focused — residential buildings + public services",
    },
    "public":      {
        "color": "#9B59B6",
        "name": "Public Services Hub",
        "desc": "Near civic amenities — public SU structures",
    },
    "mixed":       {
        "color": "#2ECC71",
        "name": "Mixed Use",
        "desc": "Tertiary streets — balanced service + residential",
    },
    "industrial":  {
        "color": "#F39C12",
        "name": "Industrial / Transit",
        "desc": "Industrial or railway-adjacent — factories, offices, transport",
    },
    "green":       {
        "color": "#1ABC9C",
        "name": "Green / STEM",
        "desc": "Parks and open space — STEM plants, residential",
    },
}

# OSM highway type → zone (ordered from highest to lowest priority in OSM)
_HIGHWAY_ZONE = {
    "motorway": "commercial",      "motorway_link": "commercial",
    "trunk": "commercial",         "trunk_link": "commercial",
    "primary": "commercial",       "primary_link": "commercial",
    "secondary": "commercial",     "secondary_link": "commercial",
    "tertiary": "mixed",           "tertiary_link": "mixed",
    "unclassified": "mixed",
    "residential": "residential",  "living_street": "residential",
    "service": "mixed",
    "pedestrian": "mixed",
    "track": "green",              "path": "green",
    "footway": "green",            "cycleway": "green",
    "steps": "green",
}

_API_DIMS_TTL = 7 * 24 * 3600  # 7 days

# ─────────────────────────────────────────────────────────────────────────────
# OSM-based zone detection
# ─────────────────────────────────────────────────────────────────────────────

def build_street_zone_map(boundary_coords: list, cache_path: Path) -> tuple[dict, dict]:
    """
    Query Overpass for named roads within the neighborhood boundary.

    Returns:
        street_zones — {NORMALIZED_STREET_NAME: zone_key}
        street_geom  — {NORMALIZED_STREET_NAME: [[lat, lon], ...]}
                       (geometry of each street's centerline for polyline rendering)

    Cache format v2: {"_v": 2, "zones": {...}, "geom": {...}}
    Cache TTL: 7 days. Falls back to ({}, {}) if Overpass unavailable.
    """
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < 7 * 86400:
            try:
                raw = json.loads(cache_path.read_text())
                if isinstance(raw, dict) and raw.get("_v") == 2:
                    zones = raw["zones"]
                    geom = raw.get("geom", {})
                    print(f"[+] Zone map from cache ({len(zones)} streets, "
                          f"{int(age/3600)}h old)")
                    return zones, geom
                # Old format (zones only) — refetch to get geometry
            except Exception:
                pass

    print("[*] Querying OSM for street classifications (Overpass)...")
    poly_str = _poly_to_overpass_str(boundary_coords)
    query = f"""
[out:json][timeout:90];
way["highway"]["name"](poly:"{poly_str}");
out geom qt;
"""
    data = _overpass_query(query, timeout=120)
    if data is None:
        print("[!] Overpass unavailable — all properties will use 'mixed' zone")
        return {}, {}

    _hw_priority = list(_HIGHWAY_ZONE.keys())

    street_highway: dict[str, str] = {}
    street_geom_raw: dict[str, list] = {}  # norm → [[lat, lon], ...]

    for elem in (data or {}).get("elements", []):
        tags = elem.get("tags", {})
        highway = tags.get("highway", "").lower()
        name = tags.get("name", "").strip().upper()
        if not highway or not name:
            continue
        tokens = [_STREET_ABBREV.get(t, t) for t in name.split()]
        norm = " ".join(tokens)

        existing = street_highway.get(norm)
        if existing is None:
            street_highway[norm] = highway
            geom_nodes = elem.get("geometry", [])
            street_geom_raw[norm] = [[pt["lat"], pt["lon"]] for pt in geom_nodes]
        else:
            new_pri = _hw_priority.index(highway) if highway in _hw_priority else 999
            old_pri = _hw_priority.index(existing) if existing in _hw_priority else 999
            if new_pri < old_pri:
                street_highway[norm] = highway
                geom_nodes = elem.get("geometry", [])
                street_geom_raw[norm] = [[pt["lat"], pt["lon"]] for pt in geom_nodes]

    street_zones = {name: _HIGHWAY_ZONE.get(hw, "mixed")
                    for name, hw in street_highway.items()}

    print(f"[+] OSM: {len(street_zones)} named streets classified")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({"_v": 2, "zones": street_zones, "geom": street_geom_raw}))
    return street_zones, street_geom_raw


def assign_zone(address: str, street_zones: dict[str, str]) -> str:
    """
    Map an Upland property address to a zone key using the OSM street zone map.
    Falls back to "mixed" if the street is not found.
    """
    addr = address.upper().strip()
    parts = addr.split(maxsplit=1)
    if len(parts) < 2:
        return "mixed"
    street_raw = parts[1]
    tokens = [_STREET_ABBREV.get(t, t) for t in street_raw.split()]
    norm = " ".join(tokens)
    if norm in street_zones:
        return street_zones[norm]
    # Partial match: check if any known key is a substring of this one
    for known, zone in street_zones.items():
        if known in norm or norm in known:
            return zone
    return "mixed"

# ─────────────────────────────────────────────────────────────────────────────
# Lot dimension fetch (generalized, neighborhood-agnostic)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_api_dims(props: list, cache_path: Path) -> dict:
    """
    Fetch lot dimensions for all properties from the public Upland API.
    Returns {UPPERCASED_ADDRESS: {up2, width_up, depth_up, fill_pct, eff_width}}.
    Cache TTL: 7 days.
    """
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            age = time.time() - cached.get("_ts", 0)
            if age < _API_DIMS_TTL:
                data = {k: v for k, v in cached.items() if k != "_ts"}
                print(f"[+] API dims from cache ({len(data)} properties, "
                      f"{int(age / 3600)}h old)")
                return data
        except Exception:
            pass

    total = len(props)
    print(f"[*] Fetching lot dimensions from Upland API ({total} properties)...")
    results: dict = {}
    done = [0]

    def _fetch_one(prop: dict) -> tuple[str, dict | None]:
        url = f"https://api.upland.me/properties/{prop['id']}"
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
            return key, {"up2": up2, "width_up": round(w, 1), "depth_up": round(d, 1),
                         "fill_pct": fill, "eff_width": eff_w}
        except Exception:
            return prop.get("address", "").upper().strip(), None

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(_fetch_one, p): p for p in props}
        for future in concurrent.futures.as_completed(futures):
            key, dims = future.result()
            if dims is not None:
                results[key] = dims
            done[0] += 1
            if done[0] % 20 == 0 or done[0] == total:
                print(f"  [{done[0]}/{total}] fetched", end="\r", flush=True)
    print()

    cache_payload = dict(results)
    cache_payload["_ts"] = time.time()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache_payload, indent=2))
    print(f"[+] API dims cached ({len(results)} properties) → {cache_path.name}")
    return results

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _darken(hex_color: str, factor: float = 0.3) -> str:
    r, g, b = int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)
    return f"#{int(r*(1-factor)):02x}{int(g*(1-factor)):02x}{int(b*(1-factor)):02x}"


def zone_polylines(street_zones: dict, street_geom: dict) -> dict:
    """
    Return {zone_key: [[[lat, lon], ...], ...]} — one polyline per street segment,
    grouped by zone. Used for drawing colored street lines on the map instead of
    overlapping filled zone polygons.
    """
    result: dict[str, list] = defaultdict(list)
    for street_name, zone_key in street_zones.items():
        line = street_geom.get(street_name)
        if line and len(line) >= 2:
            result[zone_key].append(line)
    return dict(result)

# ─────────────────────────────────────────────────────────────────────────────
# Popup HTML
# ─────────────────────────────────────────────────────────────────────────────

def _popup_html(prop: dict, structures: list, is_mine: bool,
                zone_key: str, rec: dict | None, dims: dict | None,
                username: str) -> str:
    zi = ZONE_TYPES.get(zone_key, {"color": "#888", "name": zone_key, "desc": ""})
    zone_color, zone_name = zi["color"], zi["name"]

    badge = (
        f'<span style="background:#D4A017;color:white;padding:1px 8px;'
        f'border-radius:10px;font-size:11px;margin-left:6px">YOURS</span>'
    ) if is_mine else ""

    zone_badge = (
        f'<span style="background:{zone_color};color:white;padding:1px 8px;'
        f'border-radius:10px;font-size:11px;margin-left:6px">{zone_name}</span>'
    )

    if structures:
        lines = []
        for s in structures:
            label = s.get("buildingName", "?")
            if s.get("constructionStatus") and s["constructionStatus"] != "completed":
                label += f" <i>({s['constructionStatus']})</i>"
            if s.get("buildingType"):
                label += f" <span style='color:#999;font-size:10px'>({s['buildingType']})</span>"
            lines.append(label)
        struct_html = "<br>".join(lines)
    else:
        struct_html = '<span style="color:#bbb">None (empty lot)</span>'

    size_html = ""
    if dims:
        w, d, up2 = dims["width_up"], dims["depth_up"], dims["up2"]
        eff_w = dims.get("eff_width", w)
        fill = dims.get("fill_pct", 100)
        if eff_w < 4:
            width_tag = (' <span style="background:#E74C3C;color:white;padding:1px 5px;'
                         'border-radius:8px;font-size:10px">VERY NARROW</span>')
        elif eff_w < 6:
            width_tag = (' <span style="background:#F39C12;color:white;padding:1px 5px;'
                         'border-radius:8px;font-size:10px">NARROW</span>')
        else:
            width_tag = ""
        shape_note = (f' <span style="color:#aaa;font-size:10px">({fill}% rect)</span>'
                      if fill < 80 else "")
        size_html = (
            f'<tr><td style="color:#666;padding:3px 10px 3px 0;white-space:nowrap">Size</td>'
            f'<td style="padding:3px 0">{up2} UP² '
            f'<span style="color:#888;font-size:11px">({w}^ × {d}^, eff {eff_w}^)</span>'
            f'{width_tag}{shape_note}</td></tr>'
        )

    rec_html = ""
    if rec and is_mine:
        action = rec["action"]
        if action == "KEEP":
            rec_color, icon = "#27AE60", "✓"
        elif action.startswith("DEMOLISH"):
            rec_color, icon = "#E74C3C", "⚠"
        else:
            rec_color, icon = "#F39C12", "★"
        rec_html = (
            f'<div style="margin-top:8px;padding:6px 8px;background:{rec_color}22;'
            f'border-left:3px solid {rec_color};border-radius:3px">'
            f'<span style="font-weight:bold;color:{rec_color}">{icon} {action}</span><br>'
            f'<span style="font-size:12px">{rec["desc"]}</span>'
            f'</div>'
        )

    return (
        f'<div style="font-family:Arial,sans-serif;font-size:13px;'
        f'min-width:260px;max-width:360px">'
        f'<b style="font-size:14px">{prop.get("address", "N/A")}</b>{badge}'
        f'<div style="margin-top:4px">{zone_badge}'
        f'<span style="color:#888;font-size:11px;margin-left:6px">{zi["desc"]}</span></div>'
        f'<table style="border-collapse:collapse;margin-top:8px;width:100%">'
        f'<tr><td style="color:#666;padding:3px 10px 3px 0;white-space:nowrap">Mint Price</td>'
        f'<td style="padding:3px 0">{prop.get("mintPrice", "N/A")} UPX</td></tr>'
        f'{size_html}'
        f'<tr><td style="color:#666;padding:3px 10px 3px 0;white-space:nowrap;vertical-align:top">'
        f'Structures</td>'
        f'<td style="padding:3px 0">{struct_html}</td></tr>'
        f'</table>'
        f'{rec_html}'
        f'<div style="margin-top:6px;color:#aaa;font-size:10px">ID: {prop.get("id","")}</div>'
        f'</div>'
    )

# ─────────────────────────────────────────────────────────────────────────────
# Map rendering
# ─────────────────────────────────────────────────────────────────────────────

def render_zone_map(
    hood: dict,
    props: list,
    structures: dict,
    matched: dict,
    unmatched: list,
    geocode_map: dict,
    user_ids: set,
    api_dims: dict,
    street_zones: dict,
    street_geom: dict,
    username: str,
    output_path: Path,
) -> None:
    """Render the zone optimization HTML map and save to output_path."""

    # Count structure types across the whole neighborhood for variety-aware recommendations
    neighborhood_counts: dict[str, int] = {}
    for structs_list in structures.values():
        for s in structs_list:
            name = s.get("buildingName", "")
            if name:
                neighborhood_counts[name] = neighborhood_counts.get(name, 0) + 1

    # LU balance check scoped to user-owned properties
    lu_balance = compute_lu_balance(structures, user_ids=user_ids)
    lu_deficit = lu_balance["status"] in ("lu_deficit", "lu_critical")

    def _dims(prop):
        return api_dims.get(prop.get("address", "").upper().strip())

    def _rec(prop, zone_key):
        d = _dims(prop)
        if not d:
            return None
        is_mine = str(prop.get("id", "")) in user_ids
        return auto_recommend(
            str(prop.get("id", "")),
            d.get("up2"), d.get("eff_width", d.get("width_up")), d.get("depth_up"),
            structures.get(str(prop.get("id", "")), []),
            zone_key,
            neighborhood_counts,
            lu_deficit=lu_deficit and is_mine,
        )

    # Assign zones to all properties
    prop_zones: dict[str, str] = {
        str(p["id"]): assign_zone(p.get("address", ""), street_zones)
        for p in props
    }

    center = hood.get("center", [0, 0])
    m = folium.Map(location=[center[1], center[0]], zoom_start=15,
                   tiles="CartoDB positron")

    # ── Property polygons ─────────────────────────────────────────────────────
    props_layer = folium.FeatureGroup(name="Properties", show=True)
    my_prop_ids_in_hood: set[str] = set()

    for pid, info in matched.items():
        prop = info["prop"]
        prop_id = str(prop.get("id", ""))
        is_mine = prop_id in user_ids
        if is_mine:
            my_prop_ids_in_hood.add(prop_id)
        zone_key = prop_zones.get(prop_id, "mixed")
        zi = ZONE_TYPES.get(zone_key, {"color": "#888", "name": zone_key, "desc": ""})
        zone_color = zi["color"]
        structs = structures.get(prop_id, [])
        dims = _dims(prop)
        rec = _rec(prop, zone_key)

        coords_ll = [[pt[1], pt[0]] for pt in info["coords"]]
        if is_mine:
            fill_color, border_color = zone_color, _darken(zone_color, 0.3)
            fill_opacity, weight = 0.75, 2
        else:
            fill_color, border_color = zone_color, _darken(zone_color, 0.1)
            fill_opacity, weight = 0.12, 0.5

        struct_names = ", ".join(s["buildingName"] for s in structs if s.get("buildingName"))
        tooltip = prop.get("address", "")
        if is_mine:
            tooltip += f" [{zi['name']}]"
        if struct_names:
            tooltip += f" — {struct_names}"
        if rec and is_mine:
            tooltip += f" | {rec['action']}"

        folium.Polygon(
            locations=coords_ll,
            color=border_color, weight=weight,
            fill=True, fill_color=fill_color, fill_opacity=fill_opacity,
            popup=Popup(_popup_html(prop, structs, is_mine, zone_key, rec, dims, username),
                        max_width=380),
            tooltip=tooltip,
        ).add_to(props_layer)

        if is_mine:
            cx = sum(c[0] for c in coords_ll) / len(coords_ll)
            cy = sum(c[1] for c in coords_ll) / len(coords_ll)
            if rec and rec["action"].startswith("DEMOLISH"):
                dot_color, dot_r = "#E74C3C", 4
            elif rec and rec["action"] == "BUILD":
                dot_color, dot_r = "#F39C12", 3
            elif structs:
                dot_color, dot_r = "white", 2.5
            else:
                dot_color, dot_r = zone_color, 2
            folium.CircleMarker(
                location=[cx, cy], radius=dot_r,
                color="white", weight=1,
                fill=True, fill_color=dot_color, fill_opacity=0.95,
                tooltip=struct_names or "Empty",
            ).add_to(props_layer)

    # Unmatched → geocoded circles
    for prop in unmatched:
        prop_id = str(prop["id"])
        c = geocode_map.get(prop["address"].upper().strip())
        if not c:
            continue
        lat, lon = c[0], c[1]
        is_mine = prop_id in user_ids
        if is_mine:
            my_prop_ids_in_hood.add(prop_id)
        zone_key = prop_zones.get(prop_id, "mixed")
        zi = ZONE_TYPES.get(zone_key, {"color": "#888", "name": zone_key, "desc": ""})
        zone_color = zi["color"]
        structs = structures.get(prop_id, [])
        dims = _dims(prop)
        rec = _rec(prop, zone_key) if is_mine else None

        folium.CircleMarker(
            location=[lat, lon],
            radius=6 if is_mine else 4,
            color=zone_color if is_mine else _darken(zone_color, 0.1),
            weight=2 if is_mine else 0.5,
            fill=True,
            fill_color=zone_color,
            fill_opacity=0.75 if is_mine else 0.15,
            popup=Popup(_popup_html(prop, structs, is_mine, zone_key, rec, dims, username),
                        max_width=380),
            tooltip=(f"{prop.get('address','')} [{zi['name']}]"
                     if is_mine else prop.get("address", "")),
        ).add_to(props_layer)

    props_layer.add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)

    # ── Title ─────────────────────────────────────────────────────────────────
    n_owned = len(my_prop_ids_in_hood)
    title = (
        f'<div style="position:fixed;top:10px;left:50%;transform:translateX(-50%);'
        f'background:white;padding:12px 24px;border-radius:10px;'
        f'box-shadow:0 2px 12px rgba(0,0,0,.25);z-index:1000;font-family:Arial,sans-serif">'
        f'<b style="font-size:17px">{hood["name"]} — Zone Optimization</b>'
        f'<span style="color:#666;margin-left:14px;font-size:13px">'
        f'{len(props)} properties'
        + (f' • {n_owned} owned by {username}' if username and n_owned else '')
        + f'</span></div>'
    )
    m.get_root().html.add_child(folium.Element(title))

    # ── Legend ────────────────────────────────────────────────────────────────
    zones_present = sorted({prop_zones.get(str(p["id"]), "mixed") for p in props},
                           key=lambda z: list(ZONE_TYPES.keys()).index(z)
                           if z in ZONE_TYPES else 99)
    legend_items = "".join(
        f'<div style="margin:5px 0;display:flex;align-items:center">'
        f'<span style="display:inline-block;width:16px;height:16px;'
        f'background:{ZONE_TYPES.get(zk, {}).get("color","#888")};'
        f'border-radius:3px;margin-right:8px;flex-shrink:0"></span>'
        f'<span style="font-size:12px"><b>{ZONE_TYPES.get(zk, {}).get("name", zk)}</b></span>'
        f'</div>'
        for zk in zones_present
    )
    indicator_items = (
        '<div style="margin-top:10px;padding-top:8px;border-top:1px solid #eee">'
        '<div style="margin:4px 0"><span style="color:#F39C12;font-size:14px">●</span> '
        '<span style="font-size:11px">Recommended build</span></div>'
        '<div style="margin:4px 0"><span style="color:#E74C3C;font-size:14px">●</span> '
        '<span style="font-size:11px">Demolish &amp; rebuild</span></div>'
        '<div style="margin:4px 0"><span style="color:#C0C0C0;font-size:14px">■</span> '
        '<span style="font-size:11px">Not your property</span></div>'
        '</div>'
    )
    m.get_root().html.add_child(folium.Element(
        '<div style="position:fixed;bottom:30px;right:12px;background:white;padding:14px 18px;'
        'border-radius:10px;box-shadow:0 2px 10px rgba(0,0,0,.25);z-index:1000;'
        'font-family:Arial,sans-serif;max-width:280px">'
        '<b style="display:block;margin-bottom:8px;font-size:14px">Optimization Zones</b>'
        f'{legend_items}{indicator_items}'
        '</div>'
    ))

    # ── Stats panel (only when a username is provided) ────────────────────────
    if username and n_owned > 0:
        zone_counts: dict[str, int] = defaultdict(int)
        zone_empty: dict[str, int] = defaultdict(int)
        for p in props:
            pid = str(p["id"])
            if pid not in my_prop_ids_in_hood:
                continue
            zk = prop_zones.get(pid, "mixed")
            zone_counts[zk] += 1
            if not structures.get(pid):
                zone_empty[zk] += 1
        stats_rows = "".join(
            f'<tr>'
            f'<td style="padding:2px 6px">'
            f'<span style="color:{ZONE_TYPES.get(zk,{}).get("color","#888")}">■</span> '
            f'{ZONE_TYPES.get(zk, {}).get("name", zk)}</td>'
            f'<td style="padding:2px 6px;text-align:right">{zone_counts[zk]}</td>'
            f'<td style="padding:2px 6px;text-align:right;color:#999">{zone_empty[zk]} empty</td>'
            f'</tr>'
            for zk in sorted(zone_counts, key=lambda z: zone_counts[z], reverse=True)
        )
        # LU balance warning block
        _lu_colors = {
            "balanced": ("#2ECC71", "✓"),
            "su_deficit": ("#F39C12", "⚠"),
            "lu_deficit": ("#E74C3C", "⚠"),
            "lu_critical": ("#C0392B", "✕"),
        }
        _lu_color, _lu_icon = _lu_colors.get(lu_balance["status"], ("#888", "?"))
        _lu_by_cat = lu_balance["by_cat"]
        _lu_cat_rows = "".join(
            f'<div style="font-size:10px;color:#555;margin:1px 0">'
            f'{cat}: <b>{_lu_by_cat[cat]}</b> SU '
            f'({lu_balance["ratios"].get(cat, 0):.1f}×/LU)</div>'
            for cat in ("essential", "entertainment", "public", "employment")
            if _lu_by_cat.get(cat, 0) > 0
        )
        lu_html = (
            f'<div style="margin-top:8px;padding-top:8px;border-top:1px solid #eee">'
            f'<div style="font-size:11px;font-weight:bold;color:{_lu_color};margin-bottom:3px">'
            f'{_lu_icon} SU/LU Balance</div>'
            f'<div style="font-size:11px;color:#444">{lu_balance["message"]}</div>'
            f'{_lu_cat_rows}'
            f'</div>'
        )
        m.get_root().html.add_child(folium.Element(
            '<div style="position:fixed;bottom:30px;left:12px;background:white;padding:14px 18px;'
            'border-radius:10px;box-shadow:0 2px 10px rgba(0,0,0,.25);z-index:1000;'
            'font-family:Arial,sans-serif;max-width:280px">'
            f'<b style="display:block;margin-bottom:6px;font-size:13px">'
            f'Your Properties by Zone</b>'
            f'<table style="font-size:12px;border-collapse:collapse">{stats_rows}</table>'
            f'<div style="margin-top:6px;font-size:11px;color:#888">'
            f'{n_owned} total • {sum(zone_empty.values())} empty</div>'
            f'{lu_html}'
            '</div>'
        ))

    m.save(str(output_path))
    print(f"\n[+] Zone map saved → {output_path}")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upland neighborhood zone optimization map — any neighborhood",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 zone_map.py "Dongan Hills" --city "Staten Island"
  python3 zone_map.py "Rosebank" --city "Staten Island" --username pugs08
  python3 zone_map.py "Inner Richmond" --city "San Francisco"
  python3 zone_map.py "Lincoln Park" --city "Chicago" --output-dir ~/Desktop
        """,
    )
    parser.add_argument("neighborhood", help="Neighborhood name")
    parser.add_argument("--city", help="City hint to narrow search")
    parser.add_argument("--username", default=DEFAULT_USERNAME,
                        help=f"Upland username to highlight (default: {DEFAULT_USERNAME})")
    parser.add_argument("--no-username", action="store_true",
                        help="Disable username highlighting")
    parser.add_argument("--eos-account", default=DEFAULT_EOS_ACCOUNT,
                        help=f"EOS blockchain account (default: {DEFAULT_EOS_ACCOUNT})")
    parser.add_argument("--output-dir", default="output",
                        help="Output directory (default: output/)")
    parser.add_argument("--refresh-cache", action="store_true",
                        help="Re-fetch properties and structures (clears local cache)")
    parser.add_argument("--no-geocode", action="store_true",
                        help="Skip Nominatim geocoding of unmatched properties")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = SCRIPT_DIR / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    safe_name = "".join(
        c if c.isalnum() or c in " -_" else "_" for c in args.neighborhood
    ).strip().replace(" ", "_")

    props_cache   = cache_dir / f"{safe_name}_props_cache.json"
    structs_cache = cache_dir / f"{safe_name}_structures_cache.json"
    pluto_cache   = cache_dir / f"{safe_name}_pluto_cache.json"
    geocode_cache = cache_dir / f"{safe_name}_geocode_cache.json"
    dims_cache    = cache_dir / f"{safe_name}_api_dims_cache.json"
    zone_cache    = cache_dir / f"{safe_name}_osm_zones_cache.json"
    html_path     = output_dir / f"{safe_name}_Zones.html"

    username = "" if args.no_username else args.username

    if args.refresh_cache:
        for p in [props_cache, structs_cache]:
            if p.exists():
                p.unlink()
                print(f"[*] Cleared: {p.name}")

    print(f"\n{'='*55}")
    print(f"  Zone Map: {args.neighborhood}")
    print(f"{'='*55}\n")

    # Step 1: Find neighborhood
    try:
        hood = find_neighborhood(args.neighborhood, city_hint=args.city)
    except ValueError as e:
        print(f"[!] {e}")
        sys.exit(1)

    boundary = hood.get("boundaries")
    print(f"    City:   {hood.get('city_name', 'N/A')}")
    print(f"    Area:   {hood.get('area', 0)/1e6:.2f} km²")
    if not boundary:
        print("[!] No boundary polygon — zone hulls will be unavailable; "
              "all properties assigned 'mixed'")

    # Step 2: Properties
    props = get_neighborhood_properties(
        city_id=hood["city_id"],
        neighborhood_id=hood["id"],
        neighborhood_name=hood["name"],
        cache_path=props_cache,
        boundary_coords=boundary,
    )
    if not props:
        print("[!] No properties found.")
        sys.exit(1)
    print(f"[+] {len(props)} properties in neighborhood")

    # Step 3: Structures
    structures = get_upland_property_structures(props, structs_cache)

    # Step 4: User property IDs
    user_ids: set = set()
    if username:
        bc_cache = cache_dir / f"{username}_blockchain_cache.json"
        user_ids = get_user_property_ids(
            hood["city_id"], username,
            eos_account=args.eos_account,
            blockchain_cache=bc_cache,
        )
        in_hood = sum(1 for p in props if str(p.get("id", "")) in user_ids)
        print(f"[+] {in_hood} properties owned by '{username}' in this neighborhood")

    # Step 5: Lot dimensions
    api_dims = fetch_api_dims(props, dims_cache)

    # Step 6: Building footprints (MapPLUTO for NYC, OSM buildings otherwise)
    buildings: list = []
    addr_nodes: list = []
    if boundary:
        city_name = hood.get("city_name", "")
        if _is_nyc(city_name):
            buildings = get_nyc_pluto_parcels(boundary, cache_path=pluto_cache)
            if not buildings:
                print("[~] MapPLUTO empty — falling back to OSM buildings")
                buildings, addr_nodes = get_overpass_buildings(boundary)
        else:
            buildings, addr_nodes = get_overpass_buildings(boundary)

    # Step 7: Match properties to footprints
    if buildings:
        matched, unmatched = match_to_buildings(props, buildings)
    else:
        matched, unmatched = {}, list(props)

    # Step 8: Geocode unmatched properties
    geocode_map: dict = {}
    if addr_nodes:
        geocode_map.update(_addr_nodes_to_geocode_map(addr_nodes))
    if geocode_cache.exists():
        try:
            geocode_map.update(json.loads(geocode_cache.read_text()))
        except Exception:
            pass
    still_missing = [p for p in unmatched
                     if p["address"].upper().strip() not in geocode_map]
    if still_missing and not args.no_geocode:
        nominatim_map = geocode_props(still_missing, hood.get("city_name", ""),
                                      geocode_cache)
        geocode_map.update(nominatim_map)
    elif still_missing:
        print(f"[~] {len(still_missing)} unmatched properties skipped "
              f"(--no-geocode; remove flag to place them on map)")

    # Step 9: OSM zone detection
    if boundary:
        street_zones, street_geom = build_street_zone_map(boundary, zone_cache)
    else:
        street_zones, street_geom = {}, {}

    # Apply per-neighborhood manual overrides (optional — does not require code changes).
    # Create cache/{safe_name}_zone_overrides.json with {"STREET NAME": "zone_key"} entries.
    # Overrides update street_zones (and thus the polyline colors) automatically.
    zone_override_path = cache_dir / f"{safe_name}_zone_overrides.json"
    if zone_override_path.exists():
        try:
            overrides = json.loads(zone_override_path.read_text())
            street_zones.update({k.upper().strip(): v for k, v in overrides.items()})
            print(f"[+] Loaded {len(overrides)} zone overrides from {zone_override_path.name}")
        except Exception as e:
            print(f"[~] Zone overrides file unreadable: {e}")

    # Step 10: Render map
    render_zone_map(
        hood=hood,
        props=props,
        structures=structures,
        matched=matched,
        unmatched=unmatched,
        geocode_map=geocode_map,
        user_ids=user_ids,
        api_dims=api_dims,
        street_zones=street_zones,
        street_geom=street_geom,
        username=username,
        output_path=html_path,
    )

    # Step 11: Render recommendation report
    from report import build_rows, render_report as _render_report
    prop_zones_map = {
        str(p["id"]): assign_zone(p.get("address", ""), street_zones)
        for p in props
    }
    neighborhood_counts: dict[str, int] = {}
    for structs_list in structures.values():
        for s in structs_list:
            name = s.get("buildingName", "")
            if name:
                neighborhood_counts[name] = neighborhood_counts.get(name, 0) + 1
    lu_balance = compute_lu_balance(structures, user_ids=user_ids)
    report_rows = build_rows(props, structures, user_ids, api_dims,
                             prop_zones_map, neighborhood_counts, lu_balance)
    report_path = output_dir / f"{safe_name}_Report.html"
    _render_report(args.neighborhood, report_rows, lu_balance,
                   neighborhood_counts, username, report_path)

    print(f"\n  Map:    {html_path.resolve()}")
    print(f"  Report: {report_path.resolve()}")


if __name__ == "__main__":
    main()
