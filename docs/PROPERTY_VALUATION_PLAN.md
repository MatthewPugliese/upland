# Property Valuation Tool — Plan

## Goal

Given any Upland property (by address or property ID), estimate its fair market value based on comparable recent sales. Answer the question every player asks before buying: "Is this a fair price?"

This is the most universally useful tool on the platform — every active player faces this problem daily.

---

## Status (2026-08-15)

**Not yet implemented** — no code written. Prep done: `data/economy.db` locally was a stale
snapshot (55K rows, cut off 2023-04-11 — looked like an early dev copy, predating the Pi
deployment). Replaced it with a fresh consistent copy pulled from the Pi's live database via
SQLite's online backup API (`sqlite3.Connection.backup()`, no disruption to the live scraper) —
old file kept as `data/economy.db.stale-2023-backup` in case anything needed it. Local dev/testing
against `transactions` (n5/n52 comps) now reflects real current data instead of 2023-era rows.

Next step when picked back up: build the comp-search + normalization + confidence-scoring logic
described below against this refreshed local DB, likely as `webapp/valuation.py` + a `/valuation`
route, following the same pattern as `webapp/portfolio_analyzer.py` (new module + form template +
results template, reusing existing property-cache/API-lookup helpers where possible).

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

- [ ] **Comp search** — query transactions DB for same-neighborhood, similar-UP² recent sales
- [ ] **Normalization** — compute UPX/UP² and USD/UP² for each comp, show median + range
- [ ] **Confidence scoring** — flag how many comps were found and whether city-level broadening was needed
- [ ] **Current listing comparison** — if property is currently listed, compare ask price to estimate; flag overpriced/underpriced
- [ ] **Structures adjustment** — if the property has existing structures, note their value (demolish cost, structure SU contribution) as a modifier
- [ ] **Batch mode** — paste a list of property IDs, get valuations for all (useful when evaluating multiple listings at once)
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
