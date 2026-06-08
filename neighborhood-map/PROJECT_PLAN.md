# Upland Neighborhood Optimizer — Project Plan

**Last updated:** 2026-06-08  
**Primary working dir:** `/Users/matt.pugliese/projects/local/upland/neighborhood-map/`  
**Ultimate goal:** A web app where any Upland player inputs a neighborhood + optional username and gets a fully personalized, shape-aware building recommendation breakdown for maximizing their Resident Score.

---

## What We're Building

An Upland neighborhood optimizer that:
1. Takes a **neighborhood name** and optional **username/EOS account** as input
2. Fetches all properties in the neighborhood from the Upland API
3. For each property the user owns: pulls its **actual in-game lot dimensions** (from the Upland API `boundaries` field), computes shape-adjusted effective width, and determines the best structures that physically fit
4. Assigns properties to **zones** (commercial, residential, public services, industrial, green/STEM) based on street
5. Recommends the highest-SU structure per lot, respecting zone priority and confirmed physical fit constraints
6. Renders an **interactive HTML map** + a **recommendation table** breaking down actions (KEEP / BUILD / DEMOLISH → BUILD) with SU impact

---

## Current State (as of 2026-06-08)

### Files

| File | Purpose |
|---|---|
| `neighborhood_map.py` | General-purpose neighborhood map generator (HTML + PNG). Works for any city. |
| `dongan_hills_zone_map.py` | Dongan Hills-specific zone optimization map. Generates `Dongan_Hills_Zones.html`. |
| `structure_fitter.py` | Structure database (min_up2, min_width, min_depth, SU values) + fitting logic. |
| `cache/` | Per-run caches: props, structures, pluto parcels, geocode, blockchain, API dims. |
| `dongan_hills_zone_map.py` | Zone map — currently the closest thing to the final product. |

### Architecture (current)

```
Upland API (/properties, /neighborhoods, /cities)
    → props_cache.json (property list + status)

api.upland.me/properties/{id}  (public API, no auth)
    → structures_cache.json   (buildings on each property)
    → api_dims_cache.json     (lot boundaries → width/depth/fill%)

MapPLUTO (NYC ArcGIS)
    → pluto_cache.json        (parcel polygons for OSM building outline matching)

structure_fitter.py
    → STRUCTURES dict         (min_up2, min_width, min_depth per structure)
    → structures_that_fit()   (filters by area + width + depth)
    → best_service_for_zone() (picks highest-SU structure for zone priority)
    → effective_width()       (discounts width for irregular lot shapes using MBR fill %)

dongan_hills_zone_map.py
    → STREET_ZONES            (street → zone assignment, Dongan Hills only)
    → MANUAL_OVERRIDES        (6 special-case property IDs, DH only)
    → auto_recommend()        (dynamic: uses actual dims + structure DB)
    → fetch_api_dims()        (fetches Upland API boundaries for all user props, cached 7 days)
    → popup_html()            (shows: address, zone, size w/ eff_width, structures, recommendation)
```

### Dimension System

- **Source of truth:** Upland API `boundaries` field (GeoJSON polygon per property). This IS the in-game lot.
- **UP² area:** API `area` field directly. Close to MapPLUTO but more accurate.
- **Width/depth:** Minimum Rotated Bounding Rectangle of the lot polygon, converted from meters to UP units (1 UP = 3m).
- **Effective width:** `MBR_width × sqrt(fill_pct)` — discounts irregular/jagged lots. Example: 224 Stobe Ave measures 4.2^ MBR width but only 53% rectangular → 3.1^ effective.
- **MapPLUTO:** Still used for drawing lot outlines on the map (polygon shapes). NOT used for dimension recommendations anymore.

### Structure Database Calibration Status

- **69 structures** have `min_width` entries.
- **22 confirmed** from either Playground tests or observed in-neighborhood builds.
- **47 estimated** — need Playground verification.

Key confirmed data points (from Playground testing on pugs08's Dongan Hills properties):
- Apartment Building: ≥ 6.0^ ✓ (13 observed instances)
- Day Care Center: fits at 6.0^ ✓ (door faces wrong way but counts)
- Ice Rink: fits at 8.1^, fails at 6.0^ → est. 7.0^
- Large Court House, Natural History Museum, Large Assisted Living, Public Pool, Large Day Care, DMV: all fail at 8.1^ → min_width set to 8.2^ (effectively ruled out for this neighborhood)
- Large Sports Bar, Modern Hotel, Live Theatre, Farmers Market, Brewery, Bank HQ: fail at 8.1^ → 8.2^ min
- Car Rental, Auto Repair, Try Harder Gym, Police Detention Center: barely fail at 6.0^ → 6.1^ min
- Dollar Store: fails at 4.9^ → 5.0^ min
- Funeral Home: fits at 3.2^ (675 N Railroad, anomalous lot shape) but fails at 4.2^ (224 Stobe) → 4.5^ min
- Family Home: Wonderland Season 2024 limited blueprint, expired Jan 15 2025 — removed from DB
- Small Office: confirmed fits at 4.9^ but fills most of lot

Structures NOT YET TESTED that are actively being recommended:
- Bodega (est. 4.0^), Coffee Stand (est. 4.0^), Art Gallery (est. 4.5^), Arcade (est. 4.5^), Bakery (est. 4.5^), Pool Hall (est. 5.0^), Wheel Alignment (est. 5.0^), Bike Shop (est. 4.0^), Pizzeria (est. 4.5^), Musical Instrument Store (est. 4.5^), Tire Shop (est. 4.5^), Antique Store (est. 4.0^), Town House (est. 3.9^), Apartment Building narrower limit (est. 6.0^ from observation but not Playground tested)

---

## TODO List

### 🤖 Claude-only (code work, no user input needed)

#### High Priority
- [ ] **Generalize zone assignment** — `STREET_ZONES` is currently hardcoded for Dongan Hills. Build a general zone-assignment system:
  - Fetch OSM street classifications (commercial, residential, industrial) via Overpass
  - Auto-assign zones based on street type + property density
  - Fall back to a single "General" zone with balanced SU priority
- [ ] **Extract `dongan_hills_zone_map.py` into a general `zone_map.py`** — Remove all DH-specific hardcoding (STREET_ZONES, MANUAL_OVERRIDES). Accept neighborhood name as input.
- [ ] **Build recommendation report** — In addition to the HTML map, generate a sortable HTML table:
  - Columns: Address | Zone | UP² | Width | Action | Recommended Structure | SU Gained | Current SU | Notes
  - Sorted by SU gain descending (highest impact first)
  - Filter controls: zone, action type, minimum SU gain
- [ ] **Cache the full Upland API property response** — Currently `api_dims_cache.json` only stores dimensions. Store the full response so we can also get `area`, `status`, `yield_per_hour`, and `building` without extra fetches.
- [ ] **Fetch API dims for ALL neighborhood properties** (not just user-owned) — Needed for the general web app. Rate-limit to ~10 req/sec.
- [ ] **Add `min_depth` checking to `best_service_for_zone()`** — Currently `structures_that_fit()` checks it but `best_service_for_zone()` calls `structures_that_fit()` correctly. Verify depth is threaded through.
- [ ] **Handle non-NYC cities** — MapPLUTO only covers NYC boroughs. For other cities, fall back to OSM building footprints for outline drawing (already exists in `neighborhood_map.py`). Dimension lookup should still use Upland API `boundaries`.
- [ ] **Phase scoring** — Calculate the total SU gain from each action phase so the report can show "Phase 1: +X SU, Phase 2: +Y SU"
- [ ] **Save memory** — Update memory files with key project insights (structure calibration status, web app goal, etc.)

#### Medium Priority
- [ ] **Web app backend (Flask/FastAPI)** — Endpoints:
  - `POST /analyze` — takes `{neighborhood, city, username, eos_account}`, returns JSON recommendation
  - `GET /map/{neighborhood}` — returns rendered HTML map
  - Serve the generated HTML map file
- [ ] **Web app frontend** — Simple form:
  - Neighborhood name input + city hint
  - Optional username + EOS account
  - Output: embedded interactive map + recommendation table
  - "Refresh cache" checkbox
- [ ] **Structure DB versioning** — Add a `source` field to each STRUCTURES entry: `"observed"`, `"playground_tested"`, `"estimated"`. Show source confidence in the popup.
- [ ] **Detect already-optimal properties** — If a property already has the best structure that fits, show it as "✓ OPTIMAL" instead of generic KEEP.
- [ ] **Multi-structure lot handling** — Some lots have multiple structures (e.g., Bodega + Bus Stop + Micro House). The demolish logic needs to handle partial demolition (some structures worth keeping).

#### Lower Priority
- [ ] **Dockerize** — A `Dockerfile` + `docker-compose.yml` already exists in the project root. Wire it up to the web app.
- [ ] **Auto-update structure cache on start** — If structures cache > 24h old, silently refresh in background.
- [ ] **Collection boost display** — The API returns `collection_boost`. Show in popup if > 1.
- [ ] **Yield per hour display** — Show `yield_per_hour` in popup for owned properties.

---

### 👤 User-only (Playground testing at ugc.upland.me)

The structure `min_width` database has 47 unconfirmed estimates. Test in order of how frequently they appear in recommendations.

#### Test protocol
1. Go to `ugc.upland.me`
2. Navigate to a property with the target width (see table below)
3. Try placing the structure — note FITS or FAILS
4. Report back: structure name, result, which property you tested on

#### Unconfirmed structures — test these (priority order)

**On a ~4.0–4.5^ wide lot (try 307 Seaver Ave, 304 Seaver Ave, or 129 Zoe St):**
| Structure | Est. min_width | SU | Notes |
|---|---|---|---|
| Bodega | 4.0^ | 2 ess | Most common small essential |
| Bike Shop | 4.0^ | 4 ess | Frequently recommended |
| Coffee Stand | 4.0^ | 3 ent | Very common |
| Antique Store | 4.0^ | 3 ess | Common on small lots |
| Toy Store | 4.0^ | 3 ess | Common on small lots |
| Pizzeria | 4.5^ | 4 ent | Common entertainment |
| Art Gallery | 4.5^ | 5 ent | High-value for width |
| Musical Instrument Store | 4.5^ | 4 ess | Frequently recommended |

**On a ~5.0–5.5^ wide lot (try 15 Stobe Ave at 5.4^ or 302 Buel at 5.6^):**
| Structure | Est. min_width | SU | Notes |
|---|---|---|---|
| Pool Hall | 5.0^ | 5 ent | Common entertainment |
| Wheel Alignment | 5.0^ | 5 ess | Common essential |
| Tire Shop | 4.5^ | 3 ess | Common essential |
| Arcade | 4.5^ | 3 ent | Already on a 4.8^ lot — likely fits |
| Bakery | 4.5^ | 3 ent | Already on a 4.8^ lot — likely fits |

**On a ~6.0–7.0^ wide lot (241 Buel at 5.9^, or 5 Vera St at 6.0^):**
| Structure | Est. min_width | SU | Notes |
|---|---|---|---|
| Town House | 3.9^ | res | Already observed on narrow lots |
| Micro Factory | 4.0^ | emp | Zone 5 key structure |
| Fire Station | 7.0^ | 5 pub | Already in neighborhood at 7.4^ |

**Structures known to fail (DO NOT TEST — already confirmed):**
- Large Court House, Natural History Museum, Large Assisted Living, Public Pool, Large Day Care, DMV, Farmers Market, Modern Hotel, Live Theatre, Large Sports Bar, Brewery, Bank HQ: all fail at 8.1^ wide

#### Other user-only tasks
- [ ] **Confirm what Brewery SU value actually is** — We have it as 17 SU (same as old "Local Brewery") but the in-game name is just "Brewery". Check the store listing.
- [ ] **Confirm Small Brewery SU** — We estimated 9 SU. Check in-game.
- [ ] **Check if Day Care Center door orientation affects SU scoring** — It fit at 6.0^ but the entrance faced away from the street. Does this affect Resident Score?
- [ ] **Check if Office Units contribute to Resident Score** — Research confirms they contribute to Commerce Score. But does Commerce Score feed into the overall Resident Score or are they independent metrics?
- [ ] **Check if Farm structures require special lot designation** — Can any property host farm structures or does it need to be a "farm" property type?
- [ ] **List any seasonal/event structures you own** — Family Home (Wonderland 2024) was caught. Are there others on your properties from past events?

---

### 🤝 Together (requires both)

- [ ] **Test the general map on a second neighborhood** — Run `python3 neighborhood_map.py "Rosebank" --city "Staten Island"` (cache already exists). Does it produce a sensible map? Does `auto_recommend` generalize?
- [ ] **Calibrate 3–4 more structure widths per session** — Each session, pick a lot from the table above and run through the test protocol. We'll converge on the full structure DB over ~5 sessions.
- [ ] **Validate recommendation quality on Dongan Hills** — Open the current `Dongan_Hills_Zones.html` and click through 10 properties. Do the recommendations feel right? Flag any that seem wrong.
- [ ] **Design the web app UI** — Decide: simple form + generated HTML output, or a more interactive React-style UI? Decide before Claude builds the backend.
- [ ] **Test on another player's neighborhood** — Have a friend run it on their neighborhood to find generalization bugs before we build the web app.

---

## Key Constraints & Decisions Made

| Decision | Rationale |
|---|---|
| Use Upland API `boundaries` for dimensions, not MapPLUTO | MapPLUTO dimensions match but API is authoritative and covers non-NYC |
| Use effective_width = MBR_width × sqrt(fill_pct) | Irregular lots overstate usable width; 224 Stobe Ave (53% fill) confirmed this |
| Large structures (Court House, NHM, etc.) all require > 8.1^ wide | Confirmed by exhaustive Playground testing on widest normal lot in DH |
| Zones assigned by street name | Simple, transparent, easy to generalize |
| Dynamic auto_recommend() instead of static RECOMMENDATIONS dict | Static dict had 76 incorrect recommendations due to ignored width constraints |
| 6 manual overrides only | Crown jewel (45 Vera St), Pharmacy, Arcade/Bakery (variety), Apartment+Bus (anchor), Funeral Home |
| Family Home excluded from DB | Wonderland Season 2024 limited blueprint, expired Jan 15 2025 |

## Known Technical Debt

1. `auto_recommend()` logic for demolish threshold (current_su + 8 gap) is a heuristic — needs tuning
2. `_RESIDENTIAL_ZONES` threshold (`best_su < 5`) for preferring residential is arbitrary — needs validation
3. Zone boundary convex hulls are computed from user-owned properties only — looks odd for sparse zones
4. `MANUAL_OVERRIDES` are Dongan Hills prop IDs — won't generalize to other neighborhoods (needs to become rule-based)
5. `_LOW_VALUE_TYPES` hardcoded as {Micro House, Small Town House} — should include other low-SU structures
6. Classic Hotel has `min_depth=15.0` but depth is not prominently shown in the popup (only width/eff_width shown)

## Running the Project

```bash
cd /Users/matt.pugliese/projects/local/upland/neighborhood-map

# Generate Dongan Hills zone map (uses cached data, fast):
python3 dongan_hills_zone_map.py

# Regenerate with fresh property/structure data:
python3 neighborhood_map.py "Dongan Hills" --city "Staten Island" --refresh-cache --html-only

# General neighborhood map (any city):
python3 neighborhood_map.py "Rosebank" --city "Staten Island"
python3 neighborhood_map.py "Inner Richmond" --city "San Francisco"

# Structure fitter report for Dongan Hills:
python3 structure_fitter.py
python3 structure_fitter.py --structure "Ice Rink"     # which lots fit this structure?
python3 structure_fitter.py --property "242 LIBERTY"  # what fits on this lot?

# Playground URL for manual testing:
# ugc.upland.me
```

## Environment

- Python 3.14, macOS Darwin 25.5.0
- Deps: `requests`, `folium`, `shapely`, `contextily` (optional PNG tiles)
- Upland developer API: `api.prod.upland.me/developers-api` — credentials in `upland-monitor/.env`
- Public Upland API: `api.upland.me/properties/{id}` — no auth needed
- Chain history: `chain-history.upland.me` — used for blockchain property ownership lookup
