"""
UplandScope — Map generation service

Wraps neighborhood_map.py functions to generate interactive property maps.
Two modes:
  - "simple": Property status map (owned/for-sale/locked + user highlighting)
  - "optimize": Structure optimization map with building recommendations
"""

import hashlib
import json
import sys
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

from config import (
    CACHE_DIR, MAPS_DIR, MAP_TTL_HOURS,
    MAX_CONCURRENT_GENERATIONS, NEIGHBORHOOD_MAP_DIR,
)

# ── Import neighborhood_map functions ──────────────────────────────────────

sys.path.insert(0, str(NEIGHBORHOOD_MAP_DIR))
import neighborhood_map as nm

# Try importing structure fitter
try:
    from structure_fitter import (
        STRUCTURES, compute_area_sqm, area_to_up2, compute_dimensions_up,
        structures_that_fit, best_service_for_category, normalize_addr,
    )
    HAS_FITTER = True
except ImportError:
    HAS_FITTER = False

# ── Job tracking ───────────────────────────────────────────────────────────

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_semaphore = threading.Semaphore(MAX_CONCURRENT_GENERATIONS)


def _map_key(neighborhood: str, username: str, mode: str, zones: bool = False) -> str:
    slug = "".join(
        c if c.isalnum() or c in "-_" else "_"
        for c in neighborhood.lower()
    ).strip("_")
    user_slug = username.lower() if username else "anon"
    zone_tag = "_zones" if zones else ""
    raw = f"{slug}_{user_slug}_{mode}{zone_tag}"
    return raw[:120]


def get_job(key: str) -> dict | None:
    with _jobs_lock:
        return _jobs.get(key, {}).copy()


def _update_job(key: str, **kwargs):
    with _jobs_lock:
        if key not in _jobs:
            _jobs[key] = {}
        _jobs[key].update(kwargs)


# ── Cached map check ──────────────────────────────────────────────────────

def get_cached_map(key: str) -> Path | None:
    path = MAPS_DIR / f"{key}.html"
    if path.exists():
        age_hours = (time.time() - path.stat().st_mtime) / 3600
        if age_hours < MAP_TTL_HOURS:
            return path
    return None


# ── Map generation ─────────────────────────────────────────────────────────

def request_map(neighborhood: str, city_hint: str | None,
                username: str, eos_account: str, mode: str,
                show_zones: bool = False) -> str:
    """
    Request a map generation. Returns the map key.
    If cached, marks job as ready immediately.
    Otherwise starts background generation.
    """
    key = _map_key(neighborhood, username, mode, show_zones)

    # Check cache
    cached = get_cached_map(key)
    if cached:
        _update_job(key, status="ready", path=str(cached), progress="Cached")
        return key

    # Check if already generating
    job = get_job(key)
    if job and job.get("status") == "generating":
        return key

    # Start generation
    _update_job(key, status="generating", progress="Starting...", path=None, error=None)

    t = threading.Thread(
        target=_generate_map_thread,
        args=(key, neighborhood, city_hint, username, eos_account, mode, show_zones),
        daemon=True,
    )
    t.start()
    return key


def _generate_map_thread(key: str, neighborhood: str, city_hint: str | None,
                         username: str, eos_account: str, mode: str,
                         show_zones: bool = False):
    """Background thread that generates the map."""
    _semaphore.acquire()
    try:
        if mode == "optimize" and HAS_FITTER:
            path = _generate_optimization_map(key, neighborhood, city_hint, username, eos_account, show_zones)
        else:
            path = _generate_simple_map(key, neighborhood, city_hint, username, eos_account)
        _update_job(key, status="ready", path=str(path), progress="Done")
    except Exception as e:
        import traceback
        traceback.print_exc()
        _update_job(key, status="error", error=str(e), progress="Failed")
    finally:
        _semaphore.release()


def _generate_simple_map(key: str, neighborhood: str, city_hint: str | None,
                         username: str, eos_account: str) -> Path:
    """Generate a standard property status map."""
    output_path = MAPS_DIR / f"{key}.html"
    cache_subdir = CACHE_DIR / "neighborhoods"
    cache_subdir.mkdir(parents=True, exist_ok=True)

    safe_name = "".join(
        c if c.isalnum() or c in " -_" else "_" for c in neighborhood
    ).strip().replace(" ", "_")

    cache_path = cache_subdir / f"{safe_name}_props_cache.json"
    geocode_cache = cache_subdir / f"{safe_name}_geocode_cache.json"
    pluto_cache = cache_subdir / f"{safe_name}_pluto_cache.json"
    struct_cache = cache_subdir / f"{safe_name}_structures_cache.json"

    # Step 1: Find neighborhood
    _update_job(key, progress="Finding neighborhood...")
    hood = nm.find_neighborhood(neighborhood, city_hint=city_hint)
    boundary = hood.get("boundaries")

    # Step 2: Fetch properties
    _update_job(key, progress=f"Fetching properties for {hood['name']}...")
    props = nm.get_neighborhood_properties(
        city_id=hood["city_id"],
        neighborhood_id=hood["id"],
        neighborhood_name=hood["name"],
        cache_path=cache_path,
        boundary_coords=boundary,
    )
    if not props:
        raise ValueError(f"No properties found for {hood['name']}")

    # Step 3: Building outlines
    _update_job(key, progress="Loading building outlines...")
    buildings = []
    addr_nodes = []
    city_name = hood.get("city_name", "")

    if boundary:
        if nm._is_nyc(city_name):
            buildings = nm.get_nyc_pluto_parcels(boundary, cache_path=pluto_cache)
            if not buildings:
                buildings, addr_nodes = nm.get_overpass_buildings(boundary)
        else:
            buildings, addr_nodes = nm.get_overpass_buildings(boundary)

    # Step 4: Match
    _update_job(key, progress="Matching properties to outlines...")
    if buildings:
        matched, unmatched = nm.match_to_buildings(props, buildings)
    else:
        matched, unmatched = {}, props

    # Step 5: Structures
    _update_job(key, progress="Fetching game structures (probing API)...")
    structure_data = nm.get_upland_property_structures(props, struct_cache)
    _struct_count = sum(1 for v in structure_data.values() if v)
    if _struct_count == 0:
        _update_job(key, progress="⚠ Upland API appears down — skipped structures")

    # Step 6: User properties
    user_prop_ids = set()
    if username and eos_account:
        _update_job(key, progress=f"Looking up {username}'s properties...")
        bc_cache = cache_subdir / f"{username.lower()}_blockchain_cache.json"
        user_prop_ids = nm.get_user_property_ids(
            hood["city_id"], username,
            eos_account=eos_account,
            user_props_file=None,
            blockchain_cache=bc_cache,
        )

    # Step 7: Geocode unmatched
    geocode_map = {}
    if unmatched:
        _update_job(key, progress="Geocoding unmatched properties...")
        osm_node_coords = nm._addr_nodes_to_geocode_map(addr_nodes) if addr_nodes else {}
        for prop in unmatched:
            addr_key = prop["address"].upper().strip()
            if addr_key in osm_node_coords:
                geocode_map[addr_key] = osm_node_coords[addr_key]
        still_missing = [p for p in unmatched
                         if p["address"].upper().strip() not in geocode_map]
        if still_missing:
            # Skip slow Nominatim geocoding (1 req/sec) in web app
            # Properties without outlines or OSM nodes just won't appear on map
            print(f"[~] {len(still_missing)} properties without coords — skipping Nominatim (web mode)")

    # Step 8: Render
    _update_job(key, progress="Rendering map...")
    nm.render_html_map(
        hood=hood,
        props=props,
        buildings=buildings,
        matched=matched,
        unmatched_props=unmatched,
        structure_data=structure_data,
        user_prop_ids=user_prop_ids,
        geocode_map=geocode_map,
        username=username or "",
        output_path=output_path,
    )
    return output_path


# ─────────────────────────────────────────────────────────────────────────────
# Zone classification and overlay
# ─────────────────────────────────────────────────────────────────────────────

ZONE_TYPES = {
    "Commercial":  {"color": "#E74C3C", "desc": "Shops, hotels, restaurants, entertainment"},
    "Residential": {"color": "#3498DB", "desc": "Housing, apartments, town houses"},
    "Public":      {"color": "#9B59B6", "desc": "Schools, fire stations, courts, civic buildings"},
    "Industrial":  {"color": "#F39C12", "desc": "Factories, warehouses, employment"},
    "Mixed Use":   {"color": "#1ABC9C", "desc": "Combination of residential and services"},
}

_STRUCT_ZONE_MAP = {
    "Bodega": "Commercial", "Farmers Market": "Commercial", "Modern Hotel": "Commercial",
    "Classic Hotel": "Commercial", "Try Harder Gym": "Commercial", "Dollar Store": "Commercial",
    "Auto Repair Shop": "Commercial", "Car Rental": "Commercial", "Bike Shop": "Commercial",
    "Tire Shop": "Commercial", "Pawn Shop": "Commercial", "Antique Store": "Commercial",
    "Toy Store": "Commercial", "Musical Instrument Store": "Commercial",
    "Wheel Alignment Center": "Commercial",
    "Arcade": "Commercial", "Bakery": "Commercial", "Coffee Stand": "Commercial",
    "Pizzeria": "Commercial", "Art Gallery": "Commercial", "Pool Hall": "Commercial",
    "Ice Rink": "Commercial", "Large Sports Bar": "Commercial", "Live Theatre": "Commercial",
    "Natural History Museum": "Commercial", "Ice Cream Parlor": "Commercial",
    "Kiosk - Hot Dog": "Commercial", "Donut Stand": "Commercial", "Sausage Stand": "Commercial",
    "Day Care Center": "Public", "Fire Station": "Public", "DMV": "Public",
    "Public Pool": "Public", "Large Assisted Living": "Public",
    "Large Court House": "Public", "Information Kiosk": "Public", "Bus Stop": "Mixed Use",
    "Small Factory I": "Industrial", "Small Factory II": "Industrial",
    "Micro Factory": "Industrial", "Medium Factory I": "Industrial",
    "Micro House": "Residential", "Small Town House": "Residential",
    "Town House": "Residential", "Ranch House": "Residential",
    "Luxury Ranch House": "Residential", "Luxury Modern House": "Residential",
    "Contemporary House": "Residential", "Family Home": "Residential",
    "Apartment Building": "Residential", "Glass Tower": "Residential",
}


def _classify_property_zone(address: str, structs: list) -> str:
    """
    Classify a property's zone based on both its street location and structures.
    Uses street-based heuristics to assign zones to clusters, then overrides
    with structure-based classification if the property has service/commercial builds.
    """
    addr = address.upper().strip()

    # Structure-based override: if there's a non-residential structure, classify by it
    if structs:
        zones = [_STRUCT_ZONE_MAP.get(s.get("buildingName", ""), "Mixed Use") for s in structs]
        has_commercial = "Commercial" in zones
        has_public = "Public" in zones
        has_industrial = "Industrial" in zones
        if has_industrial:
            return "Industrial"
        if has_public and not has_commercial:
            return "Public"
        if has_commercial:
            return "Commercial"

    # Street-based heuristics for zone assignment
    # Major avenues/boulevards → Commercial (these are the commercial corridors)
    commercial_keywords = ["BLVD", "HYLAN", "BROADWAY", "MAIN", "MARKET",
                          "COMMERCIAL", "ATLANTIC", "FLATBUSH"]
    for kw in commercial_keywords:
        if kw in addr:
            return "Commercial"

    # Numbered streets with "AVE" tend to be residential grid streets
    parts = addr.split(maxsplit=1)
    if len(parts) >= 2:
        street = parts[1]

        # Railroad/industrial-sounding streets
        if any(kw in street for kw in ["RAILROAD", "FACTORY", "INDUSTRIAL",
                                        "WAREHOUSE", "TERMINAL", "DOCK"]):
            return "Industrial"

        # Streets with multiple user properties and existing services → Commercial potential
        # (This will be overridden by the density clustering below)

    # Default: Residential
    return "Residential"


def _get_prop_centroid(prop, matched, geocode_map):
    pid = str(prop.get("id", ""))
    if pid in matched:
        coords = matched[pid]["coords"]
        cx = sum(pt[0] for pt in coords) / len(coords)
        cy = sum(pt[1] for pt in coords) / len(coords)
        return (cy, cx)
    addr_key = prop.get("address", "").upper().strip()
    gc = geocode_map.get(addr_key)
    if gc:
        return (gc[0], gc[1])
    return None


def _add_zone_overlays(m, props, matched, unmatched, geocode_map,
                       user_prop_ids, structure_data):
    """
    Cluster user properties spatially, then assign each cluster a zone type
    based on what structures exist there and what the optimal use would be.
    Uses street grouping first, then spatial proximity for ungrouped properties.
    """
    from shapely.geometry import MultiPoint
    import folium

    # Collect user properties with their centroids and street names
    user_props = []
    for prop in props:
        pid = str(prop.get("id", ""))
        if pid not in user_prop_ids:
            continue
        centroid = _get_prop_centroid(prop, matched, geocode_map)
        if not centroid:
            continue
        addr = prop.get("address", "").upper().strip()
        parts = addr.split(maxsplit=1)
        street = parts[1] if len(parts) >= 2 else addr
        structs = structure_data.get(pid, [])
        user_props.append({
            "pid": pid,
            "centroid": centroid,  # (lat, lon)
            "lonlat": (centroid[1], centroid[0]),  # (lon, lat) for shapely
            "street": street,
            "structs": structs,
            "address": addr,
        })

    if len(user_props) < 3:
        return

    # Group by street → these become zone candidates
    street_groups = defaultdict(list)
    for up in user_props:
        street_groups[up["street"]].append(up)

    # Assign zone types to street clusters
    # Streets with 3+ properties form their own zone
    # Smaller clusters get merged into a "scattered" zone
    zone_clusters = []  # list of {"type": str, "points": [(lon,lat)...], "count": int, "street": str}

    # Rotation of zone types for variety — assign based on cluster characteristics
    zone_type_order = ["Commercial", "Residential", "Public", "Industrial", "Mixed Use"]
    type_idx = 0

    sorted_streets = sorted(street_groups.items(), key=lambda x: -len(x[1]))

    for street, group in sorted_streets:
        if len(group) < 3:
            continue

        # Determine zone type from existing structures + street characteristics
        all_struct_types = []
        for up in group:
            for s in up["structs"]:
                z = _STRUCT_ZONE_MAP.get(s.get("buildingName", ""), "Mixed Use")
                all_struct_types.append(z)

        # Count structure types in this cluster
        type_counts = Counter(all_struct_types)

        if type_counts.get("Industrial", 0) > 0:
            zone_type = "Industrial"
        elif type_counts.get("Public", 0) >= 2:
            zone_type = "Public"
        elif type_counts.get("Commercial", 0) >= 2:
            zone_type = "Commercial"
        elif type_counts.get("Commercial", 0) > 0 and type_counts.get("Residential", 0) > 0:
            zone_type = "Mixed Use"
        else:
            # No structures or all residential → assign based on cluster position
            # Largest cluster = Commercial (main street), next = Residential, etc.
            zone_type = zone_type_order[type_idx % len(zone_type_order)]
            type_idx += 1

        # Check for industrial keywords
        if any(kw in street for kw in ["RAILROAD", "FACTORY", "INDUSTRIAL"]):
            zone_type = "Industrial"

        zone_clusters.append({
            "type": zone_type,
            "points": [up["lonlat"] for up in group],
            "count": len(group),
            "street": street,
        })

    # Small streets (< 3 properties) → group into "Scattered / Mixed Use"
    scattered = []
    for street, group in sorted_streets:
        if len(group) < 3:
            for up in group:
                scattered.append(up["lonlat"])
    if len(scattered) >= 3:
        zone_clusters.append({
            "type": "Mixed Use",
            "points": scattered,
            "count": len(scattered),
            "street": "Scattered",
        })

    # Render zone overlays
    zone_layer = folium.FeatureGroup(name="Neighborhood Zones", show=True)

    for cluster in zone_clusters:
        zinfo = ZONE_TYPES.get(cluster["type"], {"color": "#999", "desc": ""})
        color = zinfo["color"]
        points = cluster["points"]

        if len(points) < 3:
            continue

        mp = MultiPoint(points)
        hull = mp.convex_hull
        if hull.geom_type != "Polygon":
            continue

        buffered = hull.buffer(0.0004)
        if buffered.geom_type != "Polygon":
            buffered = hull

        coords = list(buffered.exterior.coords)
        latlon = [[pt[1], pt[0]] for pt in coords]

        street_label = cluster["street"]
        # Shorten long street names by dropping the trailing type suffix
        for suffix in [" AVE", " ST", " BLVD", " RD", " LN", " CT", " PL", " DR"]:
            if street_label.endswith(suffix):
                street_label = street_label[:-len(suffix)]
                break

        folium.Polygon(
            locations=latlon,
            color=color,
            weight=2.5,
            fill=True,
            fill_color=color,
            fill_opacity=0.07,
            dash_array="8 4",
            tooltip=(
                f"<b>{cluster['type']}: {street_label}</b><br>"
                f"{zinfo['desc']}<br>"
                f"{cluster['count']} of your properties"
            ),
        ).add_to(zone_layer)

        # Zone label
        label_lat = sum(p[1] for p in coords) / len(coords)
        label_lon = sum(p[0] for p in coords) / len(coords)
        folium.Marker(
            location=[label_lat, label_lon],
            icon=folium.DivIcon(
                html=(
                    f'<div style="font-size:10px;font-weight:bold;color:{color};'
                    f'text-shadow:1px 1px 2px white,-1px -1px 2px white,'
                    f'1px -1px 2px white,-1px 1px 2px white;'
                    f'white-space:nowrap;pointer-events:none;opacity:0.85">'
                    f'{cluster["type"]}: {street_label}</div>'
                ),
                icon_size=(200, 20),
                icon_anchor=(100, 10),
            ),
        ).add_to(zone_layer)

    zone_layer.add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)


def _generate_optimization_map(key: str, neighborhood: str, city_hint: str | None,
                               username: str, eos_account: str,
                               show_zones: bool = False) -> Path:
    """Generate an optimization map with structure recommendations."""
    import folium
    from folium import Popup

    output_path = MAPS_DIR / f"{key}.html"
    cache_subdir = CACHE_DIR / "neighborhoods"
    cache_subdir.mkdir(parents=True, exist_ok=True)

    safe_name = "".join(
        c if c.isalnum() or c in " -_" else "_" for c in neighborhood
    ).strip().replace(" ", "_")

    cache_path = cache_subdir / f"{safe_name}_props_cache.json"
    geocode_cache = cache_subdir / f"{safe_name}_geocode_cache.json"
    pluto_cache = cache_subdir / f"{safe_name}_pluto_cache.json"
    struct_cache = cache_subdir / f"{safe_name}_structures_cache.json"

    # Steps 1-7: same as simple mode
    _update_job(key, progress="Finding neighborhood...")
    hood = nm.find_neighborhood(neighborhood, city_hint=city_hint)
    boundary = hood.get("boundaries")

    _update_job(key, progress=f"Fetching properties for {hood['name']}...")
    props = nm.get_neighborhood_properties(
        city_id=hood["city_id"],
        neighborhood_id=hood["id"],
        neighborhood_name=hood["name"],
        cache_path=cache_path,
        boundary_coords=boundary,
    )
    if not props:
        raise ValueError(f"No properties found for {hood['name']}")

    _update_job(key, progress="Loading building outlines...")
    buildings = []
    addr_nodes = []
    city_name = hood.get("city_name", "")
    if boundary:
        if nm._is_nyc(city_name):
            buildings = nm.get_nyc_pluto_parcels(boundary, cache_path=pluto_cache)
            if not buildings:
                buildings, addr_nodes = nm.get_overpass_buildings(boundary)
        else:
            buildings, addr_nodes = nm.get_overpass_buildings(boundary)

    _update_job(key, progress="Matching properties to outlines...")
    if buildings:
        matched, unmatched = nm.match_to_buildings(props, buildings)
    else:
        matched, unmatched = {}, props

    _update_job(key, progress="Fetching game structures (probing API)...")
    structure_data = nm.get_upland_property_structures(props, struct_cache)
    _struct_count = sum(1 for v in structure_data.values() if v)
    if _struct_count == 0:
        _update_job(key, progress="⚠ Upland API appears down — skipped structures")

    user_prop_ids = set()
    if username and eos_account:
        _update_job(key, progress=f"Looking up {username}'s properties...")
        bc_cache = cache_subdir / f"{username.lower()}_blockchain_cache.json"
        user_prop_ids = nm.get_user_property_ids(
            hood["city_id"], username,
            eos_account=eos_account,
            user_props_file=None,
            blockchain_cache=bc_cache,
        )

    geocode_map = {}
    if unmatched:
        _update_job(key, progress="Geocoding unmatched properties...")
        osm_node_coords = nm._addr_nodes_to_geocode_map(addr_nodes) if addr_nodes else {}
        for prop in unmatched:
            addr_key = prop["address"].upper().strip()
            if addr_key in osm_node_coords:
                geocode_map[addr_key] = osm_node_coords[addr_key]
        still_missing = [p for p in unmatched
                         if p["address"].upper().strip() not in geocode_map]
        if still_missing:
            # Skip slow Nominatim geocoding (1 req/sec) in web app
            # Properties without outlines or OSM nodes just won't appear on map
            print(f"[~] {len(still_missing)} properties without coords — skipping Nominatim (web mode)")

    # ── Step 8: Build optimization data ───────────────────────────────────

    _update_job(key, progress="Analyzing structure optimization...")

    # Build PLUTO size index
    pluto_sizes = {}
    if buildings:
        for b in buildings:
            if b.get("house_num") and b.get("street"):
                addr_key = f"{b['house_num']} {b['street']}".upper().strip()
                sqm = compute_area_sqm(b["coords"])
                up2 = area_to_up2(sqm)
                pluto_sizes[addr_key] = up2

    def get_up2(address):
        key = normalize_addr(address)
        if key in pluto_sizes:
            return pluto_sizes[key]
        parts = key.split(maxsplit=1)
        if len(parts) == 2:
            for pk, pv in pluto_sizes.items():
                pp = pk.split(maxsplit=1)
                if len(pp) == 2 and pp[0] == parts[0] and (parts[1] in pp[1] or pp[1] in parts[1]):
                    return pv
        return 0

    # ── Step 9: Render optimization map ───────────────────────────────────

    _update_job(key, progress="Rendering optimization map...")

    center = hood.get("center", [0, 0])
    m = folium.Map(location=[center[1], center[0]], zoom_start=15,
                   tiles="CartoDB positron")

    # Neighborhood boundary
    boundary_data = hood.get("boundaries")
    if boundary_data:
        ring = boundary_data[0] if isinstance(boundary_data[0][0], (list, tuple)) else boundary_data
        folium_ring = [[pt[1], pt[0]] for pt in ring]
        folium.Polygon(
            locations=folium_ring,
            color="#2C3E50",
            weight=3,
            fill=False,
            tooltip=f"Neighborhood: {hood['name']}",
        ).add_to(m)

    # Color scheme
    USER_COLOR = "#2471A3"       # blue
    USER_EMPTY_COLOR = "#5DADE2"  # medium blue — your empty lots
    USER_BUILT_COLOR = "#1B4F72"  # navy — your lots with structures
    OTHER_OWNED = "#E8E8E8"       # very light gray — owned by others (faded)
    FOR_SALE = "#F39C12"          # orange — for sale (pops against blue+gray)
    DEFAULT = "#F0F0F0"           # near-white — unlocked/other

    STATUS_COLORS = {
        "For sale": FOR_SALE,
        "Initial Offer": "#E67E22",
        "Locked": "#CCCCCC",
        "Owned": OTHER_OWNED,
        "Unlocked": DEFAULT,
    }

    def prop_color(prop, is_user, has_structs):
        if is_user:
            if has_structs:
                return USER_BUILT_COLOR
            return USER_EMPTY_COLOR
        return STATUS_COLORS.get(prop.get("status", ""), DEFAULT)

    def opt_popup(prop, structs, is_user, up2_val):
        address = prop.get("address", "N/A")
        status = prop.get("status", "Unknown")
        mint = prop.get("mintPrice", "N/A")

        badge = ""
        if is_user:
            badge = (
                '<span style="background:#2980B9;color:white;padding:1px 8px;'
                'border-radius:10px;font-size:11px;margin-left:6px">YOURS</span>'
            )

        # Current structures
        if structs:
            struct_lines = []
            for s in structs:
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

        # Size info
        size_html = ""
        if up2_val > 0:
            size_html = f"<tr><td style='color:#666;padding:3px 10px 3px 0'>Est. Size</td><td>~{up2_val:.0f} UP2</td></tr>"

        # Recommendations (only for user properties)
        rec_html = ""
        if is_user and up2_val > 0:
            fits = structures_that_fit(up2_val)

            # Determine what this property SHOULD become
            has_service = any(s.get("buildingType") == "service" for s in structs)
            has_residential = any(s.get("buildingType") == "residential" for s in structs)
            is_empty = not structs

            # Pick the single best recommendation based on priority:
            # 1. Empty lot + big enough → highest SU service structure
            # 2. Has only residential → suggest demolish + best service
            # 3. Already has service → suggest keeping or upgrading
            top_rec = None
            action = "BUILD"

            if is_empty or (has_residential and not has_service):
                if not is_empty:
                    action = "DEMOLISH & BUILD"
                # Find the single highest-SU service that fits
                best_svc = [f for f in fits if f.get("su", 0) > 0]
                best_svc.sort(key=lambda x: -x["su"])
                if best_svc:
                    top_rec = best_svc[0]
            elif has_service:
                action = "KEEP"

            # Build the recommendation HTML
            recs = []

            if top_rec and action != "KEEP":
                su = top_rec["su"]
                cat = top_rec.get("su_cat", "service").title()
                recs.append(
                    f'<span style="color:#E74C3C;font-weight:bold">{action}:</span> '
                    f'{top_rec["name"]} ({su} {cat} SU)'
                )
            elif action == "KEEP" and structs:
                svc_names = [s["buildingName"] for s in structs if s.get("buildingType") == "service"]
                if svc_names:
                    recs.append(f'<span style="color:#27AE60;font-weight:bold">KEEP:</span> {", ".join(svc_names)}')

            # Also show top alternatives by category
            alt_lines = []
            for cat in ["essential", "entertainment", "public"]:
                best = best_service_for_category(fits, cat)
                if best:
                    top = best[0]
                    alt_lines.append(f"{cat[:3].upper()}: {top['name']} ({top['su']}SU)")
            if alt_lines:
                recs.append('<span style="color:#888;font-size:10px">Alternatives: ' + " | ".join(alt_lines) + '</span>')

            # Max residential that fits
            res_fits = [f for f in fits if f["type"] == "residential"]
            if res_fits:
                recs.append(f'<span style="color:#888;font-size:10px">Max residential: {res_fits[-1]["name"]}</span>')

            if recs:
                border_color = "#E74C3C" if "DEMOLISH" in action else ("#F39C12" if action == "BUILD" else "#27AE60")
                rec_html = (
                    f'<div style="margin-top:8px;padding:6px 8px;background:{border_color}18;'
                    f'border-left:3px solid {border_color};border-radius:3px">'
                    f'<b style="color:{border_color};font-size:12px">Recommendation:</b><br>'
                    '<span style="font-size:11px">' + "<br>".join(recs) + '</span>'
                    '</div>'
                )

        return (
            f'<div style="font-family:Arial,sans-serif;font-size:13px;min-width:260px;max-width:360px">'
            f'<b style="font-size:14px">{address}</b>{badge}'
            f'<table style="border-collapse:collapse;margin-top:6px;width:100%">'
            f'<tr><td style="color:#666;padding:3px 10px 3px 0">Status</td><td>{status}</td></tr>'
            f'<tr><td style="color:#666;padding:3px 10px 3px 0">Mint Price</td><td>{mint} UPX</td></tr>'
            f'{size_html}'
            f'<tr><td style="color:#666;padding:3px 10px 3px 0;vertical-align:top">Structures</td><td>{struct_html}</td></tr>'
            f'</table>'
            f'{rec_html}'
            f'<div style="margin-top:6px;color:#aaa;font-size:10px">ID: {prop.get("id","")}</div>'
            f'</div>'
        )

    # Render matched properties
    for pid, info in matched.items():
        prop = info["prop"]
        prop_id = str(prop.get("id", ""))
        is_user = prop_id in user_prop_ids
        structs = structure_data.get(prop_id, [])
        up2_val = get_up2(prop.get("address", ""))
        color = prop_color(prop, is_user, bool(structs))

        coords_latlon = [[pt[1], pt[0]] for pt in info["coords"]]
        struct_names = ", ".join(s["buildingName"] for s in structs if s.get("buildingName"))
        tooltip = prop.get("address", "")
        if struct_names:
            tooltip += f" — {struct_names}"
        if is_user and up2_val:
            tooltip += f" (~{up2_val:.0f} UP2)"

        folium.Polygon(
            locations=coords_latlon,
            color=color,
            weight=2.5 if is_user else 1,
            fill=True,
            fill_color=color,
            fill_opacity=0.75 if is_user else 0.5,
            popup=Popup(opt_popup(prop, structs, is_user, up2_val), max_width=380),
            tooltip=tooltip,
        ).add_to(m)

        if is_user:
            cx = sum(c[0] for c in coords_latlon) / len(coords_latlon)
            cy = sum(c[1] for c in coords_latlon) / len(coords_latlon)
            folium.CircleMarker(
                location=[cx, cy],
                radius=3,
                color="white",
                weight=1,
                fill=True,
                fill_color="white" if structs else USER_EMPTY_COLOR,
                fill_opacity=0.9,
            ).add_to(m)

    # Unmatched as circles
    for prop in unmatched:
        prop_id = str(prop["id"])
        addr_key = prop["address"].upper().strip()
        coords = geocode_map.get(addr_key)
        if not coords:
            continue
        lat, lon = coords[0], coords[1]
        is_user = prop_id in user_prop_ids
        structs = structure_data.get(prop_id, [])
        up2_val = get_up2(prop.get("address", ""))
        color = prop_color(prop, is_user, bool(structs))

        folium.CircleMarker(
            location=[lat, lon],
            radius=6 if is_user else 4,
            color=color,
            weight=2 if is_user else 1,
            fill=True,
            fill_color=color,
            fill_opacity=0.75 if is_user else 0.4,
            popup=Popup(opt_popup(prop, structs, is_user, up2_val), max_width=380),
            tooltip=prop.get("address", ""),
        ).add_to(m)

    # Title
    user_count = sum(1 for p in props if str(p.get("id", "")) in user_prop_ids) if user_prop_ids else 0
    title_html = (
        '<div style="position:fixed;top:10px;left:50%;transform:translateX(-50%);'
        'background:white;padding:10px 20px;border-radius:8px;'
        'box-shadow:0 2px 8px rgba(0,0,0,.3);z-index:1000;font-family:Arial,sans-serif">'
        f'<b style="font-size:16px">{hood["name"]} — Optimization</b>'
        f'<span style="color:#666;margin-left:12px;font-size:13px">'
        f'{len(props)} properties'
        + (f' | <b style="color:#2980B9">{username}</b>: {user_count} owned' if username else '')
        + '</span></div>'
    )
    m.get_root().html.add_child(folium.Element(title_html))

    # Legend
    legend_html = (
        '<div style="position:fixed;bottom:30px;right:12px;background:white;padding:12px 16px;'
        'border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.3);z-index:1000;'
        'font-family:Arial,sans-serif;font-size:12px">'
        '<b style="display:block;margin-bottom:6px">Legend</b>'
        f'<div style="margin:4px 0"><span style="display:inline-block;width:12px;height:12px;background:{USER_BUILT_COLOR};border-radius:2px;margin-right:6px;vertical-align:middle"></span>Your property (built)</div>'
        f'<div style="margin:4px 0"><span style="display:inline-block;width:12px;height:12px;background:{USER_EMPTY_COLOR};border-radius:2px;margin-right:6px;vertical-align:middle"></span>Your property (empty lot)</div>'
        f'<div style="margin:4px 0"><span style="display:inline-block;width:12px;height:12px;background:{FOR_SALE};border-radius:2px;margin-right:6px;vertical-align:middle"></span>For sale</div>'
        f'<div style="margin:4px 0"><span style="display:inline-block;width:12px;height:12px;background:{OTHER_OWNED};border-radius:2px;margin-right:6px;vertical-align:middle"></span>Owned (other player)</div>'
        f'<div style="margin:4px 0"><span style="display:inline-block;width:12px;height:12px;background:{DEFAULT};border-radius:2px;margin-right:6px;vertical-align:middle"></span>Unlocked / Other</div>'
        '<div style="margin-top:8px;padding-top:6px;border-top:1px solid #eee;color:#999">'
        'Click any property for details and recommendations'
        '</div></div>'
    )
    m.get_root().html.add_child(folium.Element(legend_html))

    # ── Zone overlays ────────────────────────────────────────────────────
    if show_zones and user_prop_ids:
        _add_zone_overlays(m, props, matched, unmatched, geocode_map,
                           user_prop_ids, structure_data)

    m.save(str(output_path))
    return output_path
