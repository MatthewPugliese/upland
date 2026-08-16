# Collection Tracker — Plan

## Status

### Phase 1 — Core Tracker ✅ DONE (merged PR #10)

| Component | File | Status |
|---|---|---|
| Analysis backend | `webapp/collection_tracker.py` | **Done** |
| Input form | `webapp/templates/collections.html` | **Done** |
| Results page | `webapp/templates/collections_results.html` | **Done** |
| Flask routes | `webapp/app.py` `/collections` + `/collections/run` | **Done** |
| Nav link | `webapp/templates/base.html` | **Done** |

Results are bucketed into three sections:
- **Completable now** — owns all required properties
- **Almost complete** (1–2 gap) — shows exact requirement still needed
- **In progress** — sortable/filterable table with progress %

### Phase 2 — For-Sale Finder ✅ DONE (merged PR #12)

| Component | File | Status |
|---|---|---|
| Finder backend | `webapp/forsale_finder.py` | **Done** |
| API endpoint | `webapp/app.py` `/api/collections/forsale` | **Done** |
| Session storage | `webapp/app.py` collections_run | **Done** |
| UI button + async display | `webapp/templates/collections_results.html` | **Done** |

"Find listings" button on each 1–2 away collection. Fetches async, shows matching for-sale props sorted by UPX price with address, owner, and USD price if applicable.

### Phase 4 — Yield Impact Calculator ✅ DONE (2026-07-08)

| Component | File | Status |
|---|---|---|
| Portfolio yield baseline (backend) | `webapp/app.py` `collections_run()` | **Done** |
| Owned mint sum per collection (backend) | `webapp/collection_tracker.py` `analyze_collections()` | **Done** |
| Annual yield rate input | `webapp/templates/collections.html` | **Done** |
| Yield impact panel (frontend) | `webapp/templates/collections_results.html` `renderYieldImpact()` | **Done** |

Reuses the yield model already proven in `collection_optimizer.py` (`monthly_gain = total_mint * annual_rate * (boost - 1) / 12`) instead of inventing a second one. `collections_run()` now computes the portfolio's current total mint value and current monthly/hourly UPX yield (shown as new stat cards) using a user-adjustable annual rate (defaults to 12.25%, same default as the Optimizer page). `analyze_collections()` adds `owned_mint_sum` per collection entry (sum of `mintPrice` across the properties the user already owns toward that collection).

For each "almost complete" collection, once the user clicks "Find listings," `renderYieldImpact()` picks the `gap` cheapest UPX-priced listings from the results, sums their cost and mint price, and shows: projected monthly/hourly UPX gain from completing the collection, total UPX cost to acquire the missing properties at current ask prices, and a payback period in days (or hours, if under a day). Handles three edge cases: collections with no yield boost (reward-only) hide the panel entirely; when fewer UPX-priced listings exist than the gap requires, the estimate is flagged as partial; when no UPX-priced listings exist at all, shows a note instead of a bogus $0 estimate.

Verified: Jinja renders without errors (with fake collection data through a real Flask app context), JS passes `node --check`, and `renderYieldImpact()` was unit-tested standalone for the full-data, partial-data, no-boost, and no-listings cases.

### Phase 3 — Markup & Currency Filtering ✅ DONE (2026-07-07)

| Component | File | Status |
|---|---|---|
| Fix USD/UPX price mislabeling bug | `webapp/forsale_finder.py` `_public_api_price()` | **Done** |
| Markup % + currency fields (backend) | `webapp/forsale_finder.py` `find_forsale_for_collection()` | **Done** |
| Markup/currency filter UI (frontend) | `webapp/templates/collections_results.html` `loadForSale()` / `renderForSaleListings()` | **Done** |

**Bug fixed:** `_public_api_price()` read the public API's `price` field as if it were always UPX. For USD-only listings (`on_market.currency == "USD"`), `price` actually holds the *fiat* amount — so a $5 USD listing was also showing up as "5 UPX". Fixed by branching on `on_market.currency` before assigning `price_upx`/`price_usd`. Verified against real Dongan Hills for-sale properties (one USD listing, one UPX listing).

**Backend:** `find_forsale_for_collection()` now returns every qualifying listing (not currency-filtered) with `price_upx`, `price_usd`, `currency` ("UPX"/"USD"), `markup_pct`, and `mint_price` all populated. `markup_pct = (price_upx - mint_price) / mint_price * 100`, computed only for UPX-currency listings (mint_price comes from the developers-API `mintPrice` field already present in cached candidate data — None for USD listings or when mint_price is missing/zero). Verified live against real listings.

**Frontend:** `collections_results.html` now has a filter bar per collection (`renderForSaleListings()`) with a currency select (All / UPX / USD) and sort select (Price / Markup %). Raw listings are stashed on the container element (`container._listings`) so switching filters re-renders client-side with no re-fetch — the 30-min server cache already returns everything in one call. Listings show a color-coded markup badge (green ≤100%, amber ≤1000%, red above) next to the price. Verified: template renders without Jinja errors, JS passes `node --check`, and filter/sort logic was functionally tested with simulated mixed-currency data (currency filter correctly excludes non-matching listings; markup sort correctly ranks lowest-markup first).

---

## Goal

Track which Upland collections you're contributing to, how close you are to completing each one, and which missing properties in your target collections are the cheapest to buy. Collections give a `collection_boost` multiplier to property yield — completing them is one of the highest-ROI moves in the game.

---

## The Problem

Upland has hundreds of collections. Players typically:
- Don't know which collections they're partially contributing to
- Don't know how many more properties they need to complete a collection
- Have no easy way to find the cheapest remaining properties in a collection

---

## Data Sources

- **Collection definitions**: `/api/v2/collections` from Upland API — returns all collections with `name`, `category`, `boost`, `required_count`, `property_ids`
- **Your properties**: Blockchain ownership cache (already built in neighborhood optimizer)
- **Listing prices**: `on_market` + `price` fields from Upland API per property
- **Collection boost**: already returned per-property as `collection_boost` in the API response

---

## Features

- [x] **Collection membership scan**
  - For every collection in the game, check how many of its required properties you own
  - Show: collection name | category | boost | required | you own | missing | % complete

- [x] **Completion priority ranking**
  - Rank by gap (1 away first, 2 away next, then in-progress by % complete)
  - Flag collections where you own all but 1–2 properties — highlighted in amber
  - Full results are sortable by boost, rarity, progress

- [x] **Cheapest missing properties**
  - For each target collection, list the missing properties with their current listing price (if on market)
  - Flag which ones are unlisted (harder to acquire — need to make an offer)
  - Show last known sale price for unlisted properties (from economy scraper) as a negotiation anchor

- [x] **Markup & currency filtering** — shipped 2026-07-07 (see Phase 3 above)
  - Sort missing-property listings by markup over mint price, not just absolute price
  - Filter listings by currency: UPX only / USD only / both
  - Also fixed a bug along the way where USD-only listings were mislabeled as UPX prices

- [x] **Yield impact calculator** — shipped 2026-07-08 (see Phase 4 above)
  - Current total yield/hour across all your properties
  - Projected yield/hour after completing collection X
  - Payback period: how many hours of yield does the acquisition cost represent

- [x] **Collection map** — shipped 2026-08-16 (`webapp/collection_map.py`, "View Map" button next
  to "Find listings" for near-complete collections). Plots each candidate property at its live
  centerlat/centerlng (folium circle markers, not full building-outline matching like the
  neighborhood optimizer maps — much cheaper, no OSM/geocoding pipeline needed). Colors: green =
  owned, orange = missing + for sale, red = missing + not listed. Capped at 200 plotted properties
  for large collections, prioritizing owned/for-sale first so a truncated map doesn't arbitrarily
  omit the properties that actually matter. Mac-only (folium isn't on the Pi's lightweight image),
  same accepted limitation as the other map features.

  **Found and fixed a real bug while building this:** `forsale_finder._get_neighborhood_candidates`
  had an empty-string substring bug — landmark/locked properties with no neighborhood assigned
  (e.g. Governors Island) have `neighborhood.name = None`, which becomes `""` after the fallback,
  and `"" in "ANY STRING"` is always `True` in Python — so those properties matched *every*
  neighborhood search as false positives. Fixed by requiring a non-empty neighborhood before the
  substring check. This bug affected the already-shipped "Find Listings" feature too, not just
  this new map.

  **Also discovered (documented, not fixed) a deeper, pre-existing limitation:** the same
  function's slow-path scan caps at 3,000 properties per city, but most cities have far more —
  Manhattan ~42K, several cities 300K-560K+. Neither the API's `neighborhoodId` nor `textSearch`
  params work as a server-side filter (both tested directly), so for any neighborhood without a
  precached `webapp/cache/neighborhoods/*.json` file, the scan can miss it entirely if its
  properties don't fall within the first 3,000 in the API's arbitrary ordering — confirmed live
  with "Upper East Side" in Manhattan (0 results after the false-positive fix, despite the
  neighborhood definitely having properties). Raising the cap doesn't meaningfully help (even 5x
  is still <10% coverage for the largest cities) — left as a known, documented gap rather than a
  rushed partial fix. See the docstring on `_get_neighborhood_candidates` for full detail.

- [x] **Multi-collection optimizer** — shipped 2026-07-27
  - `webapp/multi_collection_optimizer.py` — knapsack over near-complete collections with live for-sale costs
  - Scores each option: monthly yield gain / UPX cost (same yield model as Phase 4)
  - Exact subset search for ≤14 viable options; greedy+pair polish otherwise
  - API: `GET /api/collections/budget-optimize?budget=50000&fetch=true`
  - UI panel on collections results: budget input, auto-fetch listings, combo vs single-best comparison
  - Caveat from markup-reality memory: paybacks are often years — optimizer ranks, does not endorse buys

---

## UI

Lives at `/collections` in the web app. Username input at top.

```
┌────────────────────────────────────────────────────────┐
│  Username: [pugs08]  [Analyze Collections]             │
└────────────────────────────────────────────────────────┘

Summary: 127 collections contributing | 3 completable now | 12 within 1 property

┌─ Almost Complete (own all but 1–2) ───────────────────┐
│ Dongan Hills Core   8/10  boost: +15%  gap: 2 props   │
│   Missing: 101 Liberty Ave  → Listed: 12,400 UPX      │
│   Missing: 45 Vera St       → Unlisted (last: ~9k UPX)│
│                                                        │
│ Staten Island Set   4/5   boost: +8%   gap: 1 prop    │
│   Missing: 234 Hylan Blvd  → Listed: 8,900 UPX        │
└────────────────────────────────────────────────────────┘

[Full collection table — sortable by % complete, cost, yield impact]
```

---

## Dependencies

- Upland API access (collection definitions endpoint)
- Blockchain ownership cache (already built)
- Economy scraper (for last-sale price on unlisted properties) — optional but useful
- Upland API property listings (for current ask prices)

---

## Related Tools

- Property Valuation Tool (PROPERTY_VALUATION_PLAN.md) — for unlisted missing properties
- Neighborhood Optimizer (PROJECT_PLAN.md) — for-sale scanner can cross-reference collection membership
