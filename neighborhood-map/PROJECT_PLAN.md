# Upland Neighborhood Optimizer — Project Plan

**Last updated:** 2026-06-08  
**Primary working dir:** `/Users/matt.pugliese/projects/local/upland/neighborhood-map/`  
**Ultimate goal:** A web app where any Upland player inputs a neighborhood + optional username and gets a fully personalized, shape-aware building recommendation breakdown for maximizing their **Neighborhood Score** (Resident Score + Commerce Score + Influence Score).

---

## What We're Building

A web app that:
1. Takes a **neighborhood name + city** as input, plus an optional **username/EOS account**
2. Fetches **all properties** in the neighborhood (not just owned ones)
   - If no username: analyze every property as if planning the ideal neighborhood
   - If username provided: highlight owned properties, show what's built vs missing, personalize recommendations
3. For every property: pulls its **actual in-game lot dimensions** from the Upland API `boundaries` field, computes shape-adjusted effective width, determines what structures physically fit
4. Assigns properties to **zones** by street type
5. Recommends the best structures per lot across all scoring dimensions: service SU, variety, living units, greenery, commerce, employment
6. Renders an **interactive HTML map** + a **recommendation breakdown table** sorted by score impact

---

## The Scoring System (Updated Understanding)

### What "Neighborhood Score" Actually Is

The old "Neighborhood Score" was retired in late 2025. The current primary metric is **Resident Score**, which has 16 documented parameters:

**Service Units (per Living Unit ratios — 5 metrics):**
- Essential SU / Living Unit
- Entertainment SU / Living Unit
- Public SU / Living Unit
- Transportation SU / Living Unit
- Employment SU / Living Unit (factories, showrooms, MetaVentures)

**Service Structure Variety (3 metrics):**
- Essential variety — number of *different* essential structure types
- Entertainment variety — number of different entertainment types
- Public variety — number of different public types
- ⚠️ **Variety matters as much as raw SU count** — two Farmers Markets ≠ one Farmers Market + one Classic Hotel

**Resident Activity (2 metrics):**
- Active Home Addresses (players actively using home addresses here)
- All Home Addresses

**Aesthetics (4 metrics, per Living Unit):**
- Greenery / Living Unit — STEM plants, maintained with Protem/STEM feeding + petting
- Landmarks / Living Unit
- Ornaments / Living Unit — seasonal ornaments get scoring bonuses
- Decorations / Living Unit

**Infrastructure (2 metrics):**
- Residential Space / Living Unit
- Density Score — how much of minted space is developed

**Secondary scores that feed into Resident Score:**
- **Commerce Score** — Office Units from office buildings + Bonds (cross-neighborhood office placements) + Trade Routes (mid-2026). Feeds into Resident Score over time.
- **Influence Score** — service structures, employment, vehicles on lots, map assets, ornaments. Affects resident distribution.

**Farming:** Currently a separate mechanic (crop production, farm capacity). NOT documented as contributing to Resident Score yet. Status TBD for 2026.

### Key Implications for Optimization

1. **Ratios, not totals** — "SU per Living Unit" means you can't just pile on service structures. You need enough living units to keep ratios healthy.
2. **Variety bonuses** — Build different structure types within each category, not duplicates.
3. **Balanced categories** — Essential + Entertainment + Public + Transportation + Employment. Don't over-index one.
4. **Greenery is an explicit metric** — STEM plants on residential properties count. NYC = cold zone (Maple, Pine, Weeping Willow, Roses, Tulips).
5. **Weights are intentionally hidden** — Upland hasn't published exact weightings to prevent gaming.
6. **Commerce Score** — Office buildings still matter even though their direct SU = 0. Place them on industrial/commercial lots.

---

## Current State (as of 2026-06-08)

### Files

| File | Purpose |
|---|---|
| `neighborhood_map.py` | General-purpose neighborhood map generator (HTML + PNG). Works for any city. |
| `dongan_hills_zone_map.py` | Dongan Hills zone optimization map. Generates `Dongan_Hills_Zones.html`. |
| `structure_fitter.py` | Structure database (min_up2, min_width, min_depth, SU, variety category) + fitting logic. |
| `cache/` | Per-run caches: props, structures, pluto, geocode, blockchain, API dims. |
| `PROJECT_PLAN.md` | This file. |
| `DONGAN_HILLS_OPTIMIZATION.md` | Legacy zone plan + build priority reference. |

### Architecture (current)

```
Upland API (/properties, /neighborhoods, /cities)
    → props_cache.json (property list + status for whole neighborhood)

api.upland.me/properties/{id}  (public, no auth)
    → structures_cache.json   (buildings on each property)
    → api_dims_cache.json     (lot boundaries → width/depth/fill%)
      *** Currently fetches only USER-OWNED props — needs to cover ALL props ***

MapPLUTO (NYC ArcGIS)
    → pluto_cache.json        (parcel polygons for OSM building outline drawing only)

structure_fitter.py
    → STRUCTURES dict         (min_up2, min_width, min_depth per structure)
    → structures_that_fit()   (filters by area + width + depth)
    → best_service_for_zone() (highest-SU structure matching zone priority)
    → effective_width()       (MBR_width × sqrt(fill_pct) for irregular lots)

dongan_hills_zone_map.py
    → STREET_ZONES            (street → zone, DH-specific)
    → MANUAL_OVERRIDES        (6 special-case property IDs, DH-specific)
    → auto_recommend()        (dynamic: actual dims + structure DB)
    → fetch_api_dims()        (fetches Upland API boundaries, cached 7 days)
      *** Currently: user-owned props only → needs: ALL props ***
    → popup_html()            (address, zone, size w/ eff_width, structures, recommendation)
```

### Structure Database Calibration Status

**69 structures** with `min_width`. **22 confirmed** from Playground. **47 estimated.**

**Confirmed from Playground testing (pugs08, Dongan Hills):**
| Structure | min_width | Confirmed |
|---|---|---|
| Apartment Building | 6.0^ | ✓ observed 13 instances |
| Day Care Center | 6.0^ | ✓ fits (door orientation awkward) |
| Ice Rink | 7.0^ (est) | fits 8.1^, fails 6.0^ |
| Fire Station | 7.0^ (est) | ✓ observed 7.4^ |
| Small Office | 4.9^ | ✓ confirmed fits 4.9^, fills most of lot |
| Dollar Store | 5.0^ | confirmed fails 4.9^ |
| Funeral Home | 4.5^ | fits 3.2^ lot (anomalous shape), fails 4.2^ |
| Family Home | removed | Wonderland Season 2024, expired Jan 15 2025 |
| Large Court House, Natural History Museum, Large Assisted Living, Public Pool, Large Day Care, DMV, Farmers Market, Modern Hotel, Live Theatre, Large Sports Bar, Brewery, Bank HQ | 8.2^ | **all confirmed fail at 8.1^** — ruled out for DH |
| Car Rental, Auto Repair, Try Harder Gym, Police Detention Center | 6.1^ | confirmed barely fail 6.0^ |

---

## TODO List

---

### 🤖 Claude-only (code work)

#### Critical Path — Web App

- [ ] **`fetch_api_dims()` should cover ALL neighborhood properties, not just user-owned**
  - File: `dongan_hills_zone_map.py` → `fetch_api_dims(props, user_ids)`
  - Change: remove the `if str(p["id"]) in user_ids` filter, fetch for all 874 props
  - Rate-limit: 10 concurrent threads is fine, add `time.sleep(0.05)` between batches
  - Cache key: already keyed by address, just needs more entries
  - Impact: enables recommendations for any property in the neighborhood, not just pugs08's

- [ ] **Generalize zone assignment beyond Dongan Hills**
  - Current: `STREET_ZONES` is a hardcoded dict of DH street names
  - Target: query Overpass API for street classifications (highway type, landuse) within the neighborhood boundary, auto-assign zones:
    - `highway=primary/secondary` + commercial landuse → Zone 1 (Commercial)
    - Residential landuse → Zone 2 (Residential)
    - `amenity=*` density clusters → Zone 3 (Public Services)
    - Mixed use → Zone 4 (Mixed)
    - `landuse=industrial` or railway proximity → Zone 5 (Industrial)
    - Parks/green space → Zone 6 (Green/STEM)
  - Fallback: single zone "General" with balanced priority

- [ ] **Extract `dongan_hills_zone_map.py` → `zone_map.py`**
  - Accept: `neighborhood_name`, `city`, `username` (optional), `eos_account` (optional)
  - Remove: all hardcoded DH prop IDs from `MANUAL_OVERRIDES`
  - Replace `MANUAL_OVERRIDES` with rule-based detection:
    - Properties with Showrooms → KEEP (metaventure)
    - Properties with unique event structures (e.g., Speedway, seasonal) → KEEP
    - Properties already at maximum possible SU for their lot → KEEP + OPTIMAL tag

- [ ] **Add variety tracking to recommendations**
  - Track which structure types are already present in the neighborhood
  - Penalize recommending a type already well-represented; prefer new types
  - Show: "3 Farmers Markets already in neighborhood — recommend Modern Hotel for variety instead"

- [ ] **Add living unit balance check**
  - Compute current total SU and total LU for all user-owned properties
  - Warn if SU/LU ratio is very high or very low
  - Factor into recommendations: if LU is very low relative to SU, prefer residential structures

- [ ] **Build recommendation report (HTML table)**
  - Separate from the map: a sortable/filterable breakdown table
  - Columns: Address | Zone | UP² | Eff Width | Action | Recommended Structure | SU Type | SU Gain | Current Structures | Notes
  - Sort by SU gain descending (biggest wins first)
  - Filter by: zone, action type (BUILD / DEMOLISH), minimum SU gain
  - Summary row: total current SU | total potential SU | total SU gain
  - Phase breakdown: Phase 1 (highest impact) / Phase 2 / Phase 3

- [ ] **Web app backend (Flask or FastAPI)**
  - `POST /analyze` — accepts `{neighborhood, city, username?, eos_account?}`, returns map HTML + report HTML + JSON summary
  - `GET /structures` — returns the full structure DB as JSON (for frontend display)
  - Serve generated files statically
  - Input validation: neighborhood name must exist in Upland API

- [ ] **Web app frontend**
  - Simple form: neighborhood name + city hint + optional username/EOS
  - Checkbox: "Show all properties" (default ON) vs "My properties only"
  - **Zone filter toggles** — fully flexible, any combination:
    - ☑ Commercial ☑ Residential ☑ Public Services ☑ Mixed ☑ Industrial ☑ Green/STEM
    - Any single zone or any combination can be active simultaneously
    - Active zones: shown on map + included in report table
    - Inactive zones: hidden from map, excluded from report
    - One-click shortcuts: "All", "None", and a quick-select per zone ("Commercial only", "Industrial only", "Residential only", etc.)
    - Map and report update live as toggles change (no page reload)
  - Output tabs: Interactive Map | Recommendation Table | Score Breakdown
  - Mobile-friendly

- [ ] **Commerce Score layer**
  - Track office structures separately from service structures
  - Show a "Commerce" section in the recommendation report
  - Recommend: best office building that fits on industrial/commercial zone lots
  - Note: Commerce Score feeds Resident Score over time (not direct SU)

- [ ] **Greenery recommendations**
  - After residential structure is placed, recommend STEM plants based on city climate zone
  - NYC = cold zone: Maple, Pine, Weeping Willow, Roses, Tulips
  - Flag residential properties with 0 greenery

- [ ] **Cache full Upland API response per property**
  - Currently `api_dims_cache.json` stores only dimensions
  - Store full response: add `area`, `status`, `yield_per_hour`, `building`, `labels` fields
  - Saves re-fetching for structure + dimension data in one shot

- [ ] **Fix technical debt**
  - `_RESIDENTIAL_ZONES` threshold (`best_su < 5`) for preferring residential is arbitrary — tune
  - Demolish threshold (su_gain >= 8) is a heuristic — should factor in demolish cost
  - `_LOW_VALUE_TYPES` only covers Micro House + Small Town House; should auto-detect any structure whose SU is much less than what could fit
  - Zone hull computation uses user-owned props only — use all props for better zone boundaries

---

### 👤 User-only (Playground testing at ugc.upland.me)

#### Unconfirmed structures — test in priority order

**Group A — test on a ~4.0–4.5^ wide lot (307 Seaver Ave ~2.4^, 304 Seaver Ave ~2.3^, or 129 Zoe St ~2.5^ — these are very narrow, use 83 Stobe Ave at 4.8^ or 85 Stobe Ave at 4.8^)**

| Structure | Est. min_width | SU | Category |
|---|---|---|---|
| Bodega | 4.0^ | 2 | essential |
| Coffee Stand | 4.0^ | 3 | entertainment |
| Bike Shop | 4.0^ | 4 | essential |
| Antique Store | 4.0^ | 3 | essential |
| Toy Store | 4.0^ | 3 | essential |
| Bakery | 4.5^ | 3 | entertainment |
| Arcade | 4.5^ | 3 | entertainment |
| Pizzeria | 4.5^ | 4 | entertainment |
| Art Gallery | 4.5^ | 5 | entertainment |
| Musical Instrument Store | 4.5^ | 4 | essential |

**Group B — test on a ~5.0–5.5^ wide lot (15 Stobe Ave at 5.4^)**

| Structure | Est. min_width | SU | Category |
|---|---|---|---|
| Tire Shop | 4.5^ | 3 | essential |
| Pool Hall | 5.0^ | 5 | entertainment |
| Wheel Alignment Center | 5.0^ | 5 | essential |

**Group C — test on a ~4.8–5.0^ wide lot (114 Seaview at 4.9^)**

| Structure | Est. min_width | Notes |
|---|---|---|
| Micro Factory | 4.0^ | Key for Zone 5 employment |
| Office Tower | 5.0^ | Commerce Score |
| Town House | 3.9^ | Already observed at this width |

#### Other user tasks

- [ ] **Confirm Brewery SU value** — We have 17 SU. Check in-game store listing.
- [ ] **Confirm Small Brewery SU** — We estimated 9 SU. Check in-game.
- [ ] **Confirm Day Care Center door orientation** — Does facing away from street affect SU scoring?
- [ ] **Check if Office Units appear in Resident Score breakdown** — Log in, check your current score components in the Upland UI.
- [ ] **Check if farm structures require special lot designation** — Can any property host farm structures?
- [ ] **Check Greenery scoring** — Is there an in-game display showing your current Greenery score per neighborhood?
- [ ] **Check Transportation SU** — Does placing a vehicle (car, bus) on a property generate Transportation SU? What vehicle types generate the most?
- [ ] **List any other event/limited structures you own** — Check all DH properties for structures not in our DB.

---

### 🤝 Together (requires both)

- [ ] **Calibrate Group A structures** — Pick one session, test all Group A structures on 83 or 85 Stobe Ave. Report pass/fail for each.
- [ ] **Test general map on Rosebank** — Run `python3 neighborhood_map.py "Rosebank" --city "Staten Island"`. Cache exists. Does `auto_recommend` generalize reasonably?
- [ ] **Validate recommendation quality** — Open `Dongan_Hills_Zones.html`, click through 15 properties. Flag any recommendations that look wrong. We'll fix them.
- [ ] **Design the scoring dashboard** — Before Claude builds the report HTML, decide: what's the single most useful output? Ranked action list? Summary table? Score projection?
- [ ] **Test the all-properties mode** — Once `fetch_api_dims` covers all 874 DH props, check the map shows recommendations for non-owned properties too.
- [ ] **Test on another neighborhood entirely** — Try a Chicago or SF neighborhood to find generalization bugs before web app launch.

---

## Key Decisions Made

| Decision | Rationale |
|---|---|
| Upland API `boundaries` for dimensions, not MapPLUTO | API is authoritative and covers non-NYC cities |
| effective_width = MBR_width × sqrt(fill_pct) | Irregular lots (224 Stobe, 53% fill) overstate usable width |
| Large structures (Court House, NHM, etc.) ruled out at 8.1^ | Confirmed by exhaustive Playground testing |
| Dynamic auto_recommend(), not static table | Static table had 76 incorrect recommendations |
| Default: all neighborhood properties, not just user-owned | Useful for planning purchases; username makes it personal |
| Variety is a Resident Score metric, not just total SU | Official Upland docs confirm variety scoring is explicit |
| Farms not yet integrated into scoring | Not documented as contributing to Resident Score as of June 2026 |
| Weights intentionally hidden by Upland | Cannot perfectly optimize; aim for balanced coverage of all 16 parameters |

## Known Technical Debt

1. `fetch_api_dims()` currently filters to user-owned props only — must cover all props
2. `auto_recommend()` demolish threshold (su_gain >= 8) and residential preference threshold (best_su < 5) are heuristics
3. Zone boundaries computed from user props only — sparse zones look small
4. `MANUAL_OVERRIDES` are DH prop IDs — not portable to other neighborhoods
5. No variety tracking — could recommend 5 identical structure types
6. No living unit balance check — SU/LU ratio not monitored
7. `min_depth` exists in STRUCTURES for Classic Hotel but depth display in popup is secondary
8. Commerce Score (offices) treated as add-on, not first-class recommendation

## Running the Project

```bash
cd /Users/matt.pugliese/projects/local/upland/neighborhood-map

# Generate Dongan Hills zone map (uses cached data, fast):
python3 dongan_hills_zone_map.py

# Regenerate with fresh property/structure data:
python3 neighborhood_map.py "Dongan Hills" --city "Staten Island" --refresh-cache --html-only

# General map for any neighborhood:
python3 neighborhood_map.py "Rosebank" --city "Staten Island"

# Structure fitter:
python3 structure_fitter.py
python3 structure_fitter.py --structure "Ice Rink"
python3 structure_fitter.py --property "242 LIBERTY"

# Playground: ugc.upland.me
```

## Environment

- Python 3.14, macOS Darwin 25.5.0
- Deps: `requests`, `folium`, `shapely`, `contextily` (optional PNG tiles)
- Upland dev API: `api.prod.upland.me/developers-api` — credentials in `upland-monitor/.env`
- Public API: `api.upland.me/properties/{id}` — no auth
- Chain history: `chain-history.upland.me` — blockchain ownership lookup
