# Property Valuation Tool — Plan

## Goal

Given any Upland property (by address or property ID), estimate its fair market value based on comparable recent sales. Answer the question every player asks before buying: "Is this a fair price?"

This is the most universally useful tool on the platform — every active player faces this problem daily.

---

## Status (2026-08-16)

**MVP shipped** at `/valuation` in the web app (`webapp/valuation.py`, `webapp/templates/valuation.html`
+ `valuation_results.html`). Input is an in-game address (fuzzy `LIKE` match against
`scraper/property_cache.db`, with a disambiguation picker when multiple properties match) or a
numeric property ID.

**How it actually works, vs. the original design below:**
- Comp search joins `data/economy.db`'s `transactions` table straight to `scraper/property_cache.db`
  via `ATTACH DATABASE` (`t.property_id = p.prop_id`) to get each comp's neighborhood/city — the
  `transactions` table's own `neighborhood`/`city` columns are only populated for ~13% of rows
  (only what the scraper had cached at insert time), so joining through the property cache was
  necessary to get reliable coverage.
- UP² for the target and every comp comes from a live Upland API call per property (`area` field) —
  there's no size data cached anywhere at scale (`property_cache.db` only has address/neighborhood/
  city). A disk cache (`webapp/cache/valuation/area_cache.json`, 30-day TTL — UP² never changes)
  avoids re-fetching the same comps across repeat queries in a popular neighborhood.
- Broadening logic matches the confidence table below: try same-neighborhood at 90d → 180d → 365d;
  if still under 5 comps, fall back to city-level with the same window escalation. Implemented in
  `valuation.find_comps()`.
- UPX and USD valuations are computed and shown **separately** (median UPX/UP² and median USD/UP²,
  each with their own confidence tier) rather than one blended estimate — the two markets don't mix.
- If the property is currently listed, its listing price is compared against the matching-currency
  estimate and shown as a % over/under.

**Not shipped:** structures adjustment (demolish cost / SU contribution as a value modifier — no
UPX cost data exists for demolishing/replacing a structure, same gap as the Portfolio Analyzer's
"net worth" line), batch mode, neighborhood floor comparison, and no `/economy` or `/portfolio`
cross-links yet (planned in Related Tools below).

Tested against a live property (`242 LIBERTY AVE`, Dongan Hills, prop ID `81296939123819`): only 2
neighborhood comps existed in the last 90 days, correctly triggered a city-level broadening to
Staten Island (59 UPX comps, 6 USD comps, both flagged "Low — broadened" confidence) — full round
trip (live API fetch + comp query + ~60 comp-area lookups) took ~7s, dominated by the parallel
area-lookup API calls.

Prep note from the previous pass: `data/economy.db` locally had been a stale 2023-04 snapshot;
it was replaced with a fresh consistent copy pulled from the Pi's live database via SQLite's online
backup API before this was built — old file kept as `data/economy.db.stale-2023-backup`.

---

## The Core Problem

Upland has no official price guidance. Players currently:
- Guess based on mint price (unreliable — mint price doesn't reflect market)
- Ask in Discord (slow, subjective)
- Manually scan recent sales for similar properties (tedious)

A comp-based valuation model solves this with data already available from the economy scraper.

---

## How Valuation Works

### Comparable selection (in priority order)

1. **Same neighborhood + similar UP²** — properties within ±30% of the target's UP² that sold in the last 90 days in the same neighborhood
2. **Same city + similar UP²** — broaden to full city if neighborhood comps are sparse (<5 sales)
3. **UP² price per unit** — normalize all comps to UPX/UP² and USD/UP², then apply to target

### Output

```
242 LIBERTY AVE, Dongan Hills
Size: 80 UP²   Zone: Commercial

── UPX Valuation ──────────────────────────────
Comparable sales (last 90d, same neighborhood):
  68 UP²  →  9,500 UPX    (118 UPX/UP²)
  75 UP²  →  11,200 UPX   (149 UPX/UP²)
  88 UP²  →  13,800 UPX   (157 UPX/UP²)

Median comparable: 149 UPX/UP²
Estimated fair value: ~11,900 UPX
Current listing: 14,500 UPX  ⚠ 22% above estimate

── USD Valuation ──────────────────────────────
Only 1 USD comp found in neighborhood (last 90d) — too few for reliable estimate.
Broadening to Staten Island city-level: 3 comps found.
Estimated fair value: ~$38 USD
Current listing: $45 USD  ⚠ 18% above estimate
```

### Confidence tiers

| Comps found | Confidence |
|---|---|
| 10+ same neighborhood | High |
| 5–9 same neighborhood | Medium |
| <5 neighborhood, 5+ city | Low — broadened |
| <5 city | Very low — insufficient data |

---

## Data Sources

- **Sale prices**: `transactions` table from the economy scraper (n5 UPX, n52 USD)
- **Property metadata**: `property_cache.json.gz` (neighborhood, city, UP²)
- **Lot dimensions**: Upland API `boundaries` field (already fetched in neighborhood optimizer)
- **Current listing price**: `on_market` + `price` + `fiat_price` fields from Upland API

The economy scraper must be running for at least 30 days to have enough comps for most neighborhoods.

---

## Features

- [x] **Comp search** — same-neighborhood recent sales, broadening to city-level and wider time windows when sparse (90d → 180d → 365d)
- [x] **Normalization** — computes UPX/UP² and USD/UP² per comp, shows median (UPX and USD tracked separately, not blended)
- [x] **Confidence scoring** — High/Medium/Low—broadened/Very low badge based on comp count and scope, per the table above
- [x] **Current listing comparison** — if listed, shows % over/under the matching-currency estimate
- [ ] **Structures adjustment** — if the property has existing structures, note their value (demolish cost, structure SU contribution) as a modifier
- [x] **Batch mode** — `/valuation/batch`, paste addresses/IDs (one per line, capped at 25), runs
  concurrently (`valuation.estimate_batch()`, 5 workers). Ambiguous text addresses are flagged
  with an error rather than silently guessing a match — batch mode expects IDs or exact addresses.
- [ ] **Neighborhood floor comparison** — show where this property sits relative to the neighborhood floor and median

---

## UI

Lives at `/valuation` in the web app. Simple input: property address or ID.

```
┌──────────────────────────────────────────────────┐
│  Property address or ID: [________________] [Go] │
└──────────────────────────────────────────────────┘

[property card with map thumbnail, size, zone]

UPX estimate: 11,900 UPX   ████████░░  Medium confidence (7 comps)
USD estimate: ~$38          ████░░░░░░  Low confidence (3 comps, city-level)

[Comparable sales table — sortable by date, price, size]
[Current listing status]
```

---

## Dependencies

- Economy scraper running and populated (see ECONOMY_DASHBOARD_PLAN.md)
- Property cache covering the target neighborhood
- Upland API access for current listing status

---

## Related Tools

- Economy dashboard (ECONOMY_DASHBOARD_PLAN.md) — price fairness estimator feature links here
- Neighborhood optimizer (PROJECT_PLAN.md) — for-sale scanner can surface valuations inline
