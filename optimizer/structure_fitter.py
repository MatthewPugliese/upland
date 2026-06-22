#!/usr/bin/env python3
"""
Upland Structure Fitter

Computes property sizes (UP2) from MapPLUTO parcel polygon data and determines
which structures can fit on each property. Outputs a fitting report for all
properties owned by the specified user in a neighborhood.

Usage:
    python3 structure_fitter.py                    # Dongan Hills, pugs08
    python3 structure_fitter.py --json             # Output as JSON
    python3 structure_fitter.py --property "101 LIBERTY AVE"  # Single property

Size computation:
    Uses Shoelace formula on MapPLUTO parcel polygons (lon/lat) converted to
    local meters, then divided by 9 to get UP2 (1 UP2 = 9 sq meters).

Structure size data:
    - Residential: from Brian Dag's community research (empirically tested)
    - Factories: from Upland Guide (official dimensions in UP units)
    - Service: estimated from relative categories (kiosk < small < medium < large)
    - Use Upland's Playground (ugc.upland.me) to verify exact fit before building

Note: These are MINIMUM UP2 estimates. Actual fit depends on lot SHAPE (width vs
depth), not just area. Narrow lots may reject structures that fit by area alone.
Always verify in the Playground before committing Spark.
"""

import argparse
import json
import math
import sys
from pathlib import Path
from collections import defaultdict

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / "cache"

# ─────────────────────────────────────────────────────────────────────────────
# Structure database — minimum UP2 and service units
# ─────────────────────────────────────────────────────────────────────────────

STRUCTURES = {
    # min_width values: empirically observed from Dongan Hills (marked *) or estimated.
    # Always verify borderline fits in Playground before committing Spark.

    # ── Residential ──────────────────────────────────────────────────────────
    "Micro House":           {"min_up2": 5,   "min_width": 2.1, "type": "residential",  "su": 0,  "su_cat": None,            "living_units": 1},   # * observed 2.1
    "Small Town House":      {"min_up2": 13,  "min_width": 2.3, "type": "residential",  "su": 0,  "su_cat": None,            "living_units": 2},   # * observed 2.3
    "Town House":            {"min_up2": 22,  "min_width": 3.9, "type": "residential",  "su": 0,  "su_cat": None,            "living_units": 3},   # * observed 3.9
    "Ranch House":           {"min_up2": 20,  "min_width": 5.0, "type": "residential",  "su": 0,  "su_cat": None,            "living_units": 3},   # * observed 5.2, est 5.0
    "Luxury Ranch House":    {"min_up2": 25,  "min_width": 5.0, "type": "residential",  "su": 0,  "su_cat": None,            "living_units": 4},
    "Luxury Modern House":   {"min_up2": 25,  "min_width": 5.0, "type": "residential",  "su": 0,  "su_cat": None,            "living_units": 4},
    "Contemporary House":    {"min_up2": 25,  "min_width": 6.0, "type": "residential",  "su": 0,  "su_cat": None,            "living_units": 4},
    # "Family Home" — Wonderland Season 2024 limited blueprint (expired Jan 15 2025); not buildable; omitted
    "Apartment Building":    {"min_up2": 44,  "min_width": 6.0, "type": "residential",  "su": 0,  "su_cat": None,            "living_units": 8},   # * observed 6.0 (13 instances)
    "Glass Tower":           {"min_up2": 44,  "min_width": 7.0, "type": "residential",  "su": 0,  "su_cat": None,            "living_units": 10},  # * observed 7.0

    # ── Factories ────────────────────────────────────────────────────────────
    "Micro Factory":         {"min_up2": 30,  "min_width": 4.0, "type": "factory",      "su": 0,  "su_cat": "employment",    "living_units": 0},
    "Small Factory I":       {"min_up2": 86,  "min_width": 8.0, "type": "factory",      "su": 0,  "su_cat": "employment",    "living_units": 0},   # * observed 11.6

    # ── Service: Kiosks & Stands ─────────────────────────────────────────────
    "Bus Stop":              {"min_up2": 3,   "min_width": 2.0, "type": "service",      "su": 1,  "su_cat": "transportation","living_units": 0},
    "Kiosk - Hot Dog":       {"min_up2": 3,   "min_width": 2.0, "type": "service",      "su": 1,  "su_cat": "entertainment", "living_units": 0},   # * observed 2.6
    "Information Kiosk":     {"min_up2": 3,   "min_width": 2.0, "type": "service",      "su": 0.55,"su_cat": "public",       "living_units": 0},
    "Sausage Stand":         {"min_up2": 3,   "min_width": 2.0, "type": "service",      "su": 1,  "su_cat": "entertainment", "living_units": 0},
    "Donut Stand":           {"min_up2": 3,   "min_width": 2.0, "type": "service",      "su": 1,  "su_cat": "entertainment", "living_units": 0},
    "Coffee Stand":          {"min_up2": 5,   "min_width": 4.0, "type": "service",      "su": 3,  "su_cat": "entertainment", "living_units": 0},   # * observed 4.6, est 4.0

    # ── Service: Small ───────────────────────────────────────────────────────
    "Funeral Home":          {"min_up2": 12,  "min_width": 4.5, "type": "service",      "su": 3,  "su_cat": "public",        "living_units": 0},  # confirmed fails 4.2^; 675 N Railroad (3.2^) anomalous lot shape
    "Bodega":                {"min_up2": 10,  "min_width": 4.0, "type": "service",      "su": 2,  "su_cat": "essential",     "living_units": 0},   # * observed 4.0
    "Ice Cream Parlor":      {"min_up2": 10,  "min_width": 4.0, "type": "service",      "su": 2,  "su_cat": "entertainment", "living_units": 0},
    "Pawn Shop":             {"min_up2": 10,  "min_width": 4.0, "type": "service",      "su": 2,  "su_cat": "essential",     "living_units": 0},
    "Small Farmers Market":  {"min_up2": 12,  "min_width": 4.0, "type": "service",      "su": 3,  "su_cat": "essential",     "living_units": 0},
    "Barn & Nobles":         {"min_up2": 12,  "min_width": 4.0, "type": "service",      "su": 4,  "su_cat": "essential",     "living_units": 0},
    "Antique Store":         {"min_up2": 12,  "min_width": 4.0, "type": "service",      "su": 3,  "su_cat": "essential",     "living_units": 0},
    "Toy Store":             {"min_up2": 12,  "min_width": 4.0, "type": "service",      "su": 3,  "su_cat": "essential",     "living_units": 0},
    "Bike Shop":             {"min_up2": 12,  "min_width": 4.0, "type": "service",      "su": 4,  "su_cat": "essential",     "living_units": 0},
    "Bakery":                {"min_up2": 12,  "min_width": 4.5, "type": "service",      "su": 3,  "su_cat": "entertainment", "living_units": 0},   # * observed 4.8, est 4.5
    "Arcade":                {"min_up2": 15,  "min_width": 4.5, "type": "service",      "su": 3,  "su_cat": "entertainment", "living_units": 0},   # * observed 4.8, est 4.5
    "Dry Cleaner":           {"min_up2": 12,  "min_width": 4.5, "type": "service",      "su": 3,  "su_cat": "essential",     "living_units": 0},   # * observed 5.1, est 4.5
    "Fast Food Joint":       {"min_up2": 12,  "min_width": 6.5, "type": "service",      "su": 4,  "su_cat": "entertainment", "living_units": 0},   # * observed 7.0, est 6.5
    "Pizzeria":              {"min_up2": 12,  "min_width": 4.5, "type": "service",      "su": 4,  "su_cat": "entertainment", "living_units": 0},
    "Dollar Store":          {"min_up2": 15,  "min_width": 5.0, "type": "service",      "su": 5,  "su_cat": "essential",     "living_units": 0},  # confirmed fails 4.9^
    "Art Gallery":           {"min_up2": 15,  "min_width": 4.5, "type": "service",      "su": 5,  "su_cat": "entertainment", "living_units": 0},
    "Tire Shop":             {"min_up2": 15,  "min_width": 4.5, "type": "service",      "su": 3,  "su_cat": "essential",     "living_units": 0},
    "Musical Instrument Store":{"min_up2": 15,"min_width": 4.5, "type": "service",      "su": 4,  "su_cat": "essential",     "living_units": 0},

    # ── Service: Medium ──────────────────────────────────────────────────────
    "Pool Hall":             {"min_up2": 18,  "min_width": 5.0, "type": "service",      "su": 5,  "su_cat": "entertainment", "living_units": 0},
    "Wheel Alignment Center":{"min_up2": 18,  "min_width": 5.0, "type": "service",      "su": 5,  "su_cat": "essential",     "living_units": 0},
    "Auto Repair Shop":      {"min_up2": 18,  "min_width": 6.1, "type": "service",      "su": 7,  "su_cat": "essential",     "living_units": 0},  # confirmed barely fails 6.0^
    "Car Rental":            {"min_up2": 20,  "min_width": 6.1, "type": "service",      "su": 6,  "su_cat": "essential",     "living_units": 0},  # confirmed barely fails 6.0^
    "Day Care Center":       {"min_up2": 20,  "min_width": 6.0, "type": "service",      "su": 6,  "su_cat": "public",        "living_units": 0},  # * confirmed fits 6.0^ (door orientation awkward)
    "Try Harder Gym":        {"min_up2": 20,  "min_width": 6.1, "type": "service",      "su": 9,  "su_cat": "essential",     "living_units": 0},  # confirmed barely fails 6.0^
    "Fire Station":          {"min_up2": 25,  "min_width": 7.0, "type": "service",      "su": 5,  "su_cat": "public",        "living_units": 0},  # * observed 7.4, est 7.0
    "Classic Hotel":         {"min_up2": 25,  "min_width": 5.5, "type": "service",      "su": 14, "su_cat": "essential",     "min_depth": 15.0,   "living_units": 0},  # fails on DEPTH >14.3^, not width
    "Police Detention Center":{"min_up2": 30, "min_width": 6.1, "type": "service",      "su": 12, "su_cat": "public",        "living_units": 0},  # confirmed barely fails 6.0^
    "Bank Headquarters":     {"min_up2": 28,  "min_width": 8.2, "type": "service",      "su": 16, "su_cat": "essential",     "living_units": 0},  # confirmed fails 8.1^

    # ── Service: Large ───────────────────────────────────────────────────────
    "Ice Rink":              {"min_up2": 30,  "min_width": 7.0, "type": "service",      "su": 8,  "su_cat": "entertainment", "living_units": 0},  # * confirmed fits 8.1^, fails 6.0^ — est 7.0^
    "Large Sports Bar":      {"min_up2": 30,  "min_width": 8.2, "type": "service",      "su": 18, "su_cat": "entertainment", "living_units": 0},  # confirmed very close at 8.1^
    "Live Theatre":          {"min_up2": 30,  "min_width": 8.2, "type": "service",      "su": 20, "su_cat": "entertainment", "living_units": 0},  # confirmed close at 8.1^
    "Modern Hotel":          {"min_up2": 30,  "min_width": 8.2, "type": "service",      "su": 21, "su_cat": "essential",     "living_units": 0},  # confirmed close at 8.1^
    "Farmers Market":        {"min_up2": 30,  "min_width": 8.2, "type": "service",      "su": 23, "su_cat": "essential",     "living_units": 0},  # confirmed fails 8.1^
    "Brewery":               {"min_up2": 30,  "min_width": 8.2, "type": "service",      "su": 17, "su_cat": "essential",     "living_units": 0},  # confirmed barely fails 8.1^
    "Small Brewery":         {"min_up2": 18,  "min_width": 8.2, "type": "service",      "su": 9,  "su_cat": "essential",     "living_units": 0},  # confirmed fails 8.1^ (est SU)
    "DMV":                   {"min_up2": 35,  "min_width": 8.2, "type": "service",      "su": 23, "su_cat": "public",        "living_units": 0},  # confirmed fails at 8.1^
    "Large Day Care Center": {"min_up2": 40,  "min_width": 8.2, "type": "service",      "su": 29, "su_cat": "public",        "living_units": 0},  # confirmed fails at 8.1^
    "Public Pool":           {"min_up2": 40,  "min_width": 8.2, "type": "service",      "su": 31, "su_cat": "public",        "living_units": 0},  # confirmed fails at 8.1^
    "Large Assisted Living": {"min_up2": 45,  "min_width": 8.2, "type": "service",      "su": 37, "su_cat": "public",        "living_units": 0},  # confirmed fails at 8.1^
    "Natural History Museum": {"min_up2": 50, "min_width": 8.2, "type": "service",      "su": 54, "su_cat": "entertainment", "living_units": 0},  # confirmed fails at 8.1^
    "Large Court House":     {"min_up2": 50,  "min_width": 8.2, "type": "service",      "su": 62, "su_cat": "public",        "living_units": 0},  # confirmed fails at 8.1^

    # ── Offices — Commerce Score (not Resident Score); verify in Playground ──
    "Small Office":          {"min_up2": 20,  "min_width": 4.9, "type": "office",       "su": 0,  "su_cat": "commerce",      "living_units": 0},  # * confirmed fits 4.9^ but fills most of lot
    "Office Tower":          {"min_up2": 25,  "min_width": 5.0, "type": "office",       "su": 0,  "su_cat": "commerce",      "living_units": 0},
    "Large Office":          {"min_up2": 30,  "min_width": 6.0, "type": "office",       "su": 0,  "su_cat": "commerce",      "living_units": 0},
    "Office Complex":        {"min_up2": 40,  "min_width": 7.0, "type": "office",       "su": 0,  "su_cat": "commerce",      "living_units": 0},

    # ── Farm structures — crop capacity; check farm eligibility first ─────────
    "Farm Silo":             {"min_up2": 10,  "min_width": 3.0, "type": "farm",         "su": 0,  "su_cat": "farming",       "living_units": 0},
    "Farm Water Tower":      {"min_up2": 10,  "min_width": 3.0, "type": "farm",         "su": 0,  "su_cat": "farming",       "living_units": 0},
    "Heritage Barn":         {"min_up2": 25,  "min_width": 5.0, "type": "farm",         "su": 0,  "su_cat": "farming",       "living_units": 0},
    "Countryside Farmhouse": {"min_up2": 30,  "min_width": 5.0, "type": "farm",         "su": 0,  "su_cat": "farming",       "living_units": 0},
    "Modern Farm Barn":      {"min_up2": 35,  "min_width": 6.0, "type": "farm",         "su": 0,  "su_cat": "farming",       "living_units": 0},
    "Farm Equipment Shed":   {"min_up2": 35,  "min_width": 6.0, "type": "farm",         "su": 0,  "su_cat": "farming",       "living_units": 0},
}

# ─────────────────────────────────────────────────────────────────────────────
# Geometry helpers
# ─────────────────────────────────────────────────────────────────────────────

def compute_area_sqm(coords: list) -> float:
    """Compute polygon area in sq meters from [lon, lat] coordinate pairs."""
    if len(coords) < 3:
        return 0
    clat = sum(c[1] for c in coords) / len(coords)
    clon = sum(c[0] for c in coords) / len(coords)
    lat_m = 111320
    lon_m = 111320 * math.cos(math.radians(clat))
    pts = [((c[0] - clon) * lon_m, (c[1] - clat) * lat_m) for c in coords]
    n = len(pts)
    area = sum(pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1]
               for i in range(n))
    return abs(area) / 2


def lot_fill_pct(coords: list) -> float:
    """Return what fraction of the MBR the actual polygon fills (0-1). Near 1 = rectangular."""
    if len(coords) < 3:
        return 1.0
    try:
        from shapely.geometry import Polygon as SP
        clat = sum(c[1] for c in coords)/len(coords)
        clon = sum(c[0] for c in coords)/len(coords)
        lat_m = 111320; lon_m = 111320 * math.cos(math.radians(clat))
        pts = [((c[0]-clon)*lon_m, (c[1]-clat)*lat_m) for c in coords]
        poly = SP(pts).buffer(0)
        mbr = poly.minimum_rotated_rectangle
        return poly.area / mbr.area if mbr.area > 0 else 1.0
    except Exception:
        return 1.0


def effective_width(coords: list) -> float:
    """
    Width in UP units adjusted for lot shape irregularity.
    A lot that is only 50% rectangular gets its width discounted by sqrt(fill),
    since Upland fits rectangular structure footprints into the actual polygon.
    """
    w, d = compute_dimensions_up(coords)
    fill = lot_fill_pct(coords)
    return round(w * math.sqrt(fill), 1)


def compute_dimensions_up(coords: list) -> tuple[float, float]:
    """
    Compute approximate width and depth of a lot in UP units using the
    minimum rotated bounding rectangle (MBR) of the parcel polygon.

    Note: These are derived from MapPLUTO tax-lot polygons, NOT Upland's
    internal grid. Upland simplifies and snaps lot boundaries to its own
    UP grid, so actual in-game dimensions may differ. Always verify fit
    in the Upland Playground (ugc.upland.me) before building.
    """
    if len(coords) < 4:
        return 0, 0
    try:
        from shapely.geometry import Polygon as ShapelyPolygon
    except ImportError:
        # Fallback to bounding box
        clat = sum(c[1] for c in coords) / len(coords)
        lat_m = 111320
        lon_m = 111320 * math.cos(math.radians(clat))
        clon = sum(c[0] for c in coords) / len(coords)
        pts = [((c[0] - clon) * lon_m, (c[1] - clat) * lat_m) for c in coords]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return (max(xs) - min(xs)) / 3, (max(ys) - min(ys)) / 3

    clat = sum(c[1] for c in coords) / len(coords)
    clon = sum(c[0] for c in coords) / len(coords)
    lat_m = 111320
    lon_m = 111320 * math.cos(math.radians(clat))
    pts_m = [((c[0] - clon) * lon_m, (c[1] - clat) * lat_m) for c in coords]

    # Remove closing duplicate if present
    if pts_m[0] == pts_m[-1]:
        pts_m = pts_m[:-1]

    poly = ShapelyPolygon(pts_m)
    if not poly.is_valid:
        poly = poly.buffer(0)

    mbr = poly.minimum_rotated_rectangle
    mbr_coords = list(mbr.exterior.coords)
    edges = []
    for i in range(len(mbr_coords) - 1):
        dx = mbr_coords[i + 1][0] - mbr_coords[i][0]
        dy = mbr_coords[i + 1][1] - mbr_coords[i][1]
        edges.append(math.sqrt(dx * dx + dy * dy))

    if len(edges) < 2:
        return 0, 0

    width_m = min(edges[0], edges[1])
    depth_m = max(edges[0], edges[1])
    return width_m / 3, depth_m / 3


def area_to_up2(sqm: float) -> float:
    return sqm / 9


# ─────────────────────────────────────────────────────────────────────────────
# Address normalization
# ─────────────────────────────────────────────────────────────────────────────

_ABBREV = {
    "AVENUE": "AVE", "STREET": "ST", "BOULEVARD": "BLVD",
    "DRIVE": "DR", "ROAD": "RD", "LANE": "LN", "COURT": "CT",
    "PLACE": "PL", "CIRCLE": "CIR", "TERRACE": "TER",
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
}


def normalize_addr(addr: str) -> str:
    tokens = addr.upper().strip().split()
    return " ".join(_ABBREV.get(t, t) for t in tokens)


# ─────────────────────────────────────────────────────────────────────────────
# Fitting logic
# ─────────────────────────────────────────────────────────────────────────────

def structures_that_fit(up2: float, width_up: float = 0, depth_up: float = 0) -> list[dict]:
    """Return all structures whose area, width, AND depth requirements are met by this lot."""
    results = []
    for name, info in STRUCTURES.items():
        if info["min_up2"] > up2:
            continue
        if width_up and info.get("min_width", 0) > width_up:
            continue
        if depth_up and info.get("min_depth", 0) > depth_up:
            continue
        results.append({"name": name, **info})
    results.sort(key=lambda x: x["min_up2"])
    return results


def best_service_for_zone(up2: float, width_up: float, zone: str) -> dict | None:
    """
    Return the single best service structure for a lot in the given zone,
    considering both area and width constraints.

    Zone priority (what SU category to maximize first):
      Zone 1 — entertainment, then essential
      Zone 2 — public, then essential
      Zone 3 — public, then entertainment
      Zone 4 — public, then essential
      Zone 5 — essential, then employment
      Zone 6 — public, then essential
    """
    PRIORITY = {
        "Zone 1": ["entertainment", "essential", "public"],
        "Zone 2": ["public", "essential", "entertainment"],
        "Zone 3": ["public", "entertainment", "essential"],
        "Zone 4": ["public", "essential", "entertainment"],
        "Zone 5": ["essential", "public", "entertainment"],
        "Zone 6": ["public", "essential", "entertainment"],
    }
    cats = PRIORITY.get(zone, ["essential", "entertainment", "public"])
    fits = [s for s in structures_that_fit(up2, width_up) if s["type"] == "service" and s["su"] > 0]
    if not fits:
        return None
    # Score: primary category rank (lower = better), then SU descending
    def score(s):
        try:
            rank = cats.index(s["su_cat"])
        except ValueError:
            rank = len(cats)
        return (rank, -s["su"])
    return min(fits, key=score)


def best_service_for_category(fits: list[dict], category: str) -> list[dict]:
    """From structures that fit, return the best (highest SU) for a category."""
    matching = [s for s in fits if s.get("su_cat") == category and s["su"] > 0]
    matching.sort(key=lambda x: -x["su"])
    return matching


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Upland structure fitter")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--property", help="Filter to a single property address")
    parser.add_argument("--min-up2", type=float, default=0, help="Filter properties >= this UP2")
    parser.add_argument("--structure", help="Show which properties can fit this structure")
    args = parser.parse_args()

    # Load data
    with open(CACHE_DIR / "Dongan_Hills_pluto_cache.json") as f:
        pluto = json.load(f)
    with open(CACHE_DIR / "Dongan_Hills_props_cache.json") as f:
        props = json.load(f)
    with open(CACHE_DIR / "Dongan_Hills_structures_cache.json") as f:
        structs = json.load(f)
    with open(CACHE_DIR / "pugs08_blockchain_cache.json") as f:
        bc = json.load(f)

    user_ids = {str(pid) for pid in bc["owned"]}

    # Build PLUTO index
    pluto_by_addr = {}
    for p in pluto:
        if p.get("house_num") and p.get("street"):
            key = f"{p['house_num']} {p['street']}".upper().strip()
            sqm = compute_area_sqm(p["coords"])
            w, d = compute_dimensions_up(p["coords"])
            pluto_by_addr[key] = {
                "up2": area_to_up2(sqm),
                "sqm": sqm,
                "width_up": w,
                "depth_up": d,
            }

    def get_size(addr):
        key = normalize_addr(addr)
        if key in pluto_by_addr:
            return pluto_by_addr[key]
        parts = key.split(maxsplit=1)
        if len(parts) == 2:
            for pk, pv in pluto_by_addr.items():
                pp = pk.split(maxsplit=1)
                if len(pp) == 2 and pp[0] == parts[0] and (parts[1] in pp[1] or pp[1] in parts[1]):
                    return pv
        return None

    # Filter to user properties
    my_props = [p for p in props if str(p["id"]) in user_ids]

    # Enrich with sizes
    enriched = []
    for p in my_props:
        addr = p.get("address", "")
        pid = str(p["id"])
        size = get_size(addr)
        current = structs.get(pid, [])
        fits = structures_that_fit(size["up2"]) if size else []

        entry = {
            "id": pid,
            "address": addr,
            "mint_price": p.get("mintPrice", 0),
            "up2": round(size["up2"]) if size else None,
            "sqm": round(size["sqm"]) if size else None,
            "width_up": round(size["width_up"], 1) if size else None,
            "depth_up": round(size["depth_up"], 1) if size else None,
            "current_structures": [s["buildingName"] for s in current],
            "fits_residential": [s["name"] for s in fits if s["type"] == "residential"],
            "fits_service": [s["name"] for s in fits if s["type"] == "service"],
            "fits_factory": [s["name"] for s in fits if s["type"] == "factory"],
            "fits_office": [s["name"] for s in fits if s["type"] == "office"],
            "fits_farm": [s["name"] for s in fits if s["type"] == "farm"],
            "best_essential": [s["name"] for s in best_service_for_category(fits, "essential")[:3]],
            "best_entertainment": [s["name"] for s in best_service_for_category(fits, "entertainment")[:3]],
            "best_public": [s["name"] for s in best_service_for_category(fits, "public")[:3]],
            "total_fits": len(fits),
        }
        enriched.append(entry)

    enriched.sort(key=lambda x: -(x["up2"] or 0))

    # Filter
    if args.property:
        target = args.property.upper()
        enriched = [e for e in enriched if target in e["address"].upper()]
    if args.min_up2:
        enriched = [e for e in enriched if (e["up2"] or 0) >= args.min_up2]

    # Structure search mode
    if args.structure:
        target_struct = args.structure
        sinfo = STRUCTURES.get(target_struct)
        if not sinfo:
            # Fuzzy match
            for sn in STRUCTURES:
                if target_struct.lower() in sn.lower():
                    target_struct = sn
                    sinfo = STRUCTURES[sn]
                    break
        if not sinfo:
            print(f"Unknown structure: {args.structure}")
            print(f"Available: {', '.join(sorted(STRUCTURES.keys()))}")
            sys.exit(1)

        can_fit = [e for e in enriched if (e["up2"] or 0) >= sinfo["min_up2"]]
        cannot_fit = [e for e in enriched if (e["up2"] or 0) < sinfo["min_up2"]]

        print(f"\n=== Properties that CAN fit: {target_struct} (needs {sinfo['min_up2']}+ UP2) ===\n")
        for e in can_fit:
            current = ", ".join(e["current_structures"]) or "empty"
            print(f"  {e['address']:<30} {e['up2']:>4} UP2  [{current}]")
        print(f"\n  {len(can_fit)} of {len(enriched)} properties can fit this structure")

        if cannot_fit:
            print(f"\n=== TOO SMALL ({len(cannot_fit)} properties) ===\n")
            for e in cannot_fit[:10]:
                print(f"  {e['address']:<30} {e['up2']:>4} UP2  (needs {sinfo['min_up2'] - (e['up2'] or 0):.0f} more)")
        return

    # Output
    if args.json:
        print(json.dumps(enriched, indent=2))
        return

    # Table output
    print(f"\n{'='*90}")
    print(f"  STRUCTURE FITTER — Pugs08 Dongan Hills ({len(enriched)} properties)")
    print(f"{'='*90}\n")

    for e in enriched:
        up2 = e["up2"] or "?"
        dims = ""
        if e["width_up"] and e["depth_up"]:
            dims = f" ({e['width_up']}^ x {e['depth_up']}^)"
        current = ", ".join(e["current_structures"]) or "EMPTY"

        print(f"  {e['address']:<30}  {up2:>4} UP2{dims}")
        print(f"  Current: {current}")

        # Largest residential
        if e["fits_residential"]:
            print(f"  Max residential: {e['fits_residential'][-1]}")

        # Best service per category
        for cat, key in [("Essential", "best_essential"), ("Entertainment", "best_entertainment"),
                         ("Public Svc", "best_public")]:
            best = e.get(key, [])
            if best:
                top = best[0]
                su = STRUCTURES[top]["su"]
                print(f"  Best {cat}: {top} ({su} SU)")

        # Factory
        if e["fits_factory"]:
            print(f"  Factory: {e['fits_factory'][-1]}")

        # Office (Commerce Score)
        if e["fits_office"]:
            print(f"  Office (Commerce): {e['fits_office'][-1]}")

        # Farm (crop capacity)
        if e["fits_farm"]:
            print(f"  Farm (crop capacity): {e['fits_farm'][-1]}")

        print()


if __name__ == "__main__":
    main()
