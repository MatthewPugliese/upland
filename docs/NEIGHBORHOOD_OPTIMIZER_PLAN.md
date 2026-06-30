# Upland Neighborhood Optimizer — Project Plan

**Last updated:** 2026-06-30  
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

## Current State (as of 2026-06-30)

### Files

| File | Purpose |
|---|---|
| `neighborhood_map.py` | General-purpose neighborhood map generator (HTML + PNG). Works for any city. |
| `zone_map.py` | **Generalized** zone optimization map — works for any neighborhood via OSM auto-zoning. |
| `dongan_hills_zone_map.py` | Dongan Hills–specific zone map (hardcoded STREET_ZONES + MANUAL_OVERRIDES). Kept for DH precision. |
| `structure_fitter.py` | Structure database (min_up2, min_width, min_depth, SU, variety category) + fitting logic. |
| `recommender.py` | `auto_recommend()`, `_rule_based_action()` (portable Showroom/unknown-structure KEEP logic). |
| `cache/` | Per-run caches: props, structures, pluto, geocode, blockchain, API dims, OSM zone map. |
| `DONGAN_HILLS_OPTIMIZATION.md` | Legacy zone plan + build priority reference. |

### Architecture (current)

```
Upland API (/properties, /neighborhoods, /cities)
    → props_cache.json (property list + status for whole neighborhood)

api.upland.me/properties/{id}  (public, no auth)
    → structures_cache.json   (buildings on each property)
    → api_dims_cache.json     (lot boundaries → width/depth/fill%, ALL props covered)

Overpass API (OSM)
    → osm_zones_cache.json    (street name → zone key, auto-detected per neighborhood)

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

- [x] **`fetch_api_dims()` covers ALL neighborhood properties** — 874/874 DH props in cache

- [x] **Generalized zone assignment — `zone_map.py`**
  - Queries Overpass for highway-tagged streets within the neighborhood boundary
  - Maps `highway=primary/secondary` → `commercial`, `residential` → `residential`, etc.
  - Six generic zones: `commercial`, `residential`, `public`, `mixed`, `industrial`, `green`
  - Caches result as `{safe_name}_osm_zones_cache.json` (7-day TTL)
  - Falls back to `"mixed"` for streets not in OSM or when Overpass is unavailable
  - `structure_fitter.best_service_for_zone()` supports both generic and legacy "Zone N" keys
  - `recommender._RESIDENTIAL_ZONES` includes `"residential"` and `"green"`

- [x] **Extracted `zone_map.py`** — accepts any neighborhood name + city + optional username
  - `_rule_based_action()` in `recommender.py` replaces hardcoded `MANUAL_OVERRIDES`:
    - Showroom in any structure name → KEEP (MetaVenture, never demolish)
    - Unknown structure name (not in STRUCTURES DB) → KEEP (limited/event blueprint)
  - `dongan_hills_zone_map.py` remains for DH-specific precision (keeps hardcoded STREET_ZONES)

- [x] **Add variety tracking to recommendations**
  - `recommender.py:generate_report()` computes `neighborhood_counts` (dict of name→count across all built structures)
  - `auto_recommend()` + `best_service_for_zone()` accept `neighborhood_counts`; prefer count==0 types, then least-duplicated
  - Popup shows `" [new type]"` / `" [3× in nbhd]"` tags in recommendation description
  - `zone_map.py:render_zone_map()` computes and threads `neighborhood_counts` through the `_rec()` closure

- [x] **Add living unit balance check**
  - `recommender.compute_lu_balance(structures, user_ids)` — computes total_lu, total_su, per-category SU, ratios, status, and a human-readable message
  - Status: `"balanced"` | `"su_deficit"` (SU/LU < 2) | `"lu_deficit"` (SU/LU > 12) | `"lu_critical"` (0 LU built)
  - `auto_recommend()` accepts `lu_deficit=True` — lowers the service-over-residential threshold from 5 SU to 10 SU and treats all zones as residential-eligible when LU is critically low
  - `generate_report()` computes balance and passes `lu_deficit` only for owned properties (not neighbors)
  - `zone_map.render_zone_map()` shows LU balance warning panel in bottom-left stats box (color-coded: green = balanced, orange = su_deficit, red = lu_deficit, dark red = lu_critical)

- [x] **Build recommendation report (HTML table)**
  - `report.py` — self-contained HTML, all filtering/sorting client-side JS
  - Columns: Address | Zone | UP² | Eff Width | Action | Recommendation | SU Type | SU Gain | Current Structures
  - Filters: zone buttons, action buttons, mine-only toggle, min SU gain input
  - Sort any column by clicking header; default sort: SU gain descending
  - Summary metrics: total SU gain, current SU, potential SU, build/demolish/keep counts, SU/LU balance
  - Auto-generated alongside the zone map by `zone_map.py`; also runnable standalone

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

- [x] **Current Score Dashboard** — shipped as standalone `/score` page (PR #14)
  - SU breakdown by category (essential, entertainment, public, transportation) with progress bars vs targets
  - SU/LU ratios, variety counts, density %, employment building count
  - "Biggest gaps" section with shortfall numbers and action suggestions
  - Full building inventory table (type, SU, LU per unit)
  - Neighborhood tabs to switch between any neighborhood with a structures cache
  - No live API calls — computed from structures cache built during map generation
  - Not yet: greenery/STEM scoring, residents count (requires per-property API call)

- [x] **Building image thumbnails** — shipped PR #16
  - CDN: `https://static.upland.me/3d-models/{buildingImage}`
  - Score Dashboard inventory: 48px thumbnail per row with graceful fallback
  - Map popups: 36px inline thumbnail next to each structure name
  - `building_images.py` caches name→path; auto-fetches new types
  - `neighborhood_map.py` now stores `buildingImage` in structures cache

- [ ] **In-game building footprint rendering**
  - The API returns `polygon`, `lat/lng`, `scale`, and `rotate` for every placed structure
  - Use this to draw the actual in-game building footprint on each lot in the map, instead of OSM outlines
  - More accurate than OSM — shows exactly where the building sits on the lot and how much space remains
  - Render as a filled overlay on top of the lot polygon

- [ ] **For-sale scanner**
  - Fetch `on_market` and `price` fields from the full API response for all unowned properties in the neighborhood
  - Surface a "Buy opportunities" panel: unowned lots currently listed for sale, ranked by potential SU gain if purchased
  - Show: address, current price (UPX), lot size, best structure that fits, SU gain, price-per-SU efficiency
  - Helps prioritize what to buy next vs what to build on existing lots

- [ ] **Spark hours estimator**
  - The API returns `stepSparks` and `minStackedSparks` per structure in the `details` field
  - Given the full recommended build queue, compute total spark hours needed end-to-end
  - Show per-structure spark cost and a running total
  - Flag structures that are spark-heavy relative to their SU gain

- [ ] **Residents tracker**
  - The API returns a `residents` count per building
  - Surface total residents across all user properties
  - Flag residential buildings with 0 residents — these are underperforming housing units
  - Show trend if data is refreshed over time (residents gained/lost since last fetch)

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

- [ ] **Plan completion tracker**
  - Compute % of recommended actions completed (BUILD done, DEMOLISH done) vs total in plan
  - Show at the top of the Score Breakdown tab: "32% of plan complete — 58 actions remaining"
  - Break down by phase: Phase 1 (X/10 done), Phase 2 (X/Y done)

- [ ] **Build cost planner**
  - For every recommended structure in the action queue, surface its real-money cost (from structure DB) and spark hours estimate
  - Show total plan cost: "$142 USD + 4,200 spark hours remaining"
  - Sortable by cost-efficiency: SU gained per dollar spent

- [ ] **"What if" build simulator**
  - Let the user select any empty lot and any structure from a dropdown
  - Show how SU/LU ratios and variety counts change if that structure were built
  - No API call needed — runs entirely off cached data + structure DB

- [ ] **Multi-owner coordination view**
  - For each property in the neighborhood NOT owned by the user, show what the current owner has built
  - Highlight gaps where a neighboring player's lot is empty — useful for identifying coordination opportunities
  - Could surface: "your neighbor at 244 Liberty Ave has an empty 52 UP² lot — if they built X it would help the whole neighborhood"

- [ ] **Ornaments and decorations tracker**
  - Aesthetics (Ornaments / LU, Decorations / LU) are explicit Resident Score metrics
  - Surface which residential properties have 0 ornaments/decorations
  - Flag seasonal ornament opportunities (ornaments get scoring bonuses during events)

- [ ] **Shareable neighborhood report**
  - One-page HTML summary of the neighborhood state: score breakdown, top 5 recommended actions, zone map thumbnail
  - Shareable URL or exportable as PNG — useful for Discord/community posts

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
5. ~~No variety tracking~~ — fixed: `neighborhood_counts` threaded through recommender
6. ~~No living unit balance check~~ — fixed: `compute_lu_balance()` + `lu_deficit` flag in recommender
7. `min_depth` exists in STRUCTURES for Classic Hotel but depth display in popup is secondary
8. Commerce Score (offices) treated as add-on, not first-class recommendation

## Running the Project

```bash
cd /Users/matt.pugliese/projects/local/upland/optimizer

# Generalized zone map — works for ANY neighborhood:
python3 zone_map.py "Dongan Hills" --city "Staten Island"
python3 zone_map.py "Rosebank" --city "Staten Island" --username pugs08
python3 zone_map.py "Inner Richmond" --city "San Francisco"
python3 zone_map.py "Lincoln Park" --city "Chicago" --output-dir ~/Desktop

# DH-specific zone map (hardcoded street zones, higher precision for DH):
python3 dongan_hills_zone_map.py

# General status map for any neighborhood (no zones/recommendations):
python3 neighborhood_map.py "Rosebank" --city "Staten Island"
python3 neighborhood_map.py "Dongan Hills" --city "Staten Island" --refresh-cache --html-only

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
