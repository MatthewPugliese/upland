# Portfolio Analyzer — Plan

## Goal

Given any Upland username, produce a full breakdown of their portfolio: properties owned, yield income, structures built, neighborhoods active in, and overall efficiency. Useful for analyzing your own portfolio and scouting other players.

---

## Status (2026-08-15)

**MVP shipped** at `/portfolio` in the web app (`webapp/portfolio_analyzer.py`, `webapp/templates/portfolio.html` + `portfolio_results.html`). Reuses `load_user_properties()` (blockchain ownership + property cache, same as Collection Tracker) and adds one new per-property API pass (`fetch_property_details`) that pulls UP² + placed buildings together in a single request per property (24h cache at `webapp/cache/portfolio/{username}_details_cache.json`).

Shipped: portfolio summary, neighborhood breakdown, structure inventory (SU via `score_calculator._lookup`), undeveloped-properties list. Not shipped: per-property yield efficiency ranking, income tracker, spark usage tracker, spark manager sub-feature, scouting-another-player mode — see notes below on why and what each needs.

**Why no per-property yield ranking:** every "yield" figure elsewhere in this repo (Collection Tracker, budget optimizer) is `mintPrice × flat assumed annual rate` — there's no real per-property yield data anywhere in the codebase. Under a flat rate, yield is strictly proportional to mint price, so a per-property ranking would just re-sort by mint price — not useful. A real ranking needs actual UPX income per property, which means ingesting `n31` (yield-collection) blockchain events with amounts, which the economy scraper does not currently do (`n31` is only used opportunistically today, purely to reconstruct the *current owned-property-ID set* from the `p55` field in `_blockchain_user_properties` — the transfer amount inside each `n31` transaction, needed for a real income figure, is never read). See `docs/ECONOMY_DASHBOARD_PLAN.md`'s income tracker section for the same gap.

**Scouting another player** is now unblocked: `username_lookup.lookup_eos_account()` (added same session) builds a lazy in-memory reverse index over `data/username_cache.json`, so `/portfolio` resolves any of the ~207k known usernames to an EOS account automatically when the EOS Account field is left blank. Falls back to a clean error if the username isn't in the cache (e.g. never minted/never active on-chain) — user can supply the EOS account directly in that case. Not yet wired up: the plan's "read-only, no spark details" restriction for scouting mode — today the scouted view is identical to the owner's view (moot for now since spark data isn't shown anywhere yet either).

---

## Features

### Your own portfolio view (`/portfolio?user=pugs08`)

- [x] **Portfolio summary** *(partial — see below)*
  - [x] Total properties owned, total mint value
  - [ ] Total current market value (estimated from comps) — needs Property Valuation Tool
  - [x] Total yield per hour/day/month *(flat-rate estimate only, see Status above)*
  - [x] Total UP² owned, % developed (has at least one structure)
  - [ ] Net worth estimate: mint value + structure replacement cost — needs per-structure build cost data

- [ ] **Yield efficiency ranking**
  - For each property: yield/hour, mint price, yield/hour per UPX invested
  - Rank from best to worst ROI — shows which properties are underperforming
  - Flag properties with 0 yield (unlisted and unbuilt — dead weight)

- [x] **Neighborhood breakdown**
  - Which neighborhoods are you active in, how many properties per neighborhood
  - For each neighborhood: your contribution %, structures built, total SU added
  - Link to neighborhood optimizer for each

- [ ] **Income tracker**
  - Pull `n31` (yield collection) events from the blockchain for the user
  - Show total UPX collected per day/week/month
  - Chart: yield income over time — did it go up as you built more?

- [ ] **Spark usage tracker**
  - Pull spark stacking/unstacking events from blockchain
  - Show which properties are currently consuming spark and at what rate
  - Flag properties with spark running low (below minimum stacked threshold)
  - Total spark burn rate per day across all properties

- [x] **Structure inventory**
  - Full list of every structure across all properties, grouped by type
  - Total SU by category across all your neighborhoods
  - Structures not yet built vs structures in construction

### Scouting another player (`/portfolio?user=someOtherPlayer`)

Same view but read-only and limited to public data (no spark details). Useful for:
- Assessing a potential trade partner's portfolio strength
- Seeing what a major neighborhood landlord has built
- Identifying whales who are active in your target neighborhoods

---

## Spark Manager (sub-feature)

Spark management is one of the most tedious parts of Upland. This sub-feature surfaces it clearly.

- [ ] **Spark allocation view**
  - For each property with a structure: structure name, spark stacked, min required, max allowed, days until depletion at current rate
  - Sort by "days remaining" — urgent refuels at the top
  - Show total spark across all properties

- [ ] **Refuel priority list**
  - Properties below minimum stacked threshold flagged in red
  - Properties within 7 days of depletion in yellow
  - Estimated spark needed to top up all properties to max

---

## Data Sources

- **Property ownership**: Blockchain cache (`chain-history.upland.me`) — already used in neighborhood optimizer
- **Structures**: Upland API per property — already cached
- **Yield events**: Hyperion API filtering `n31` actions for the target EOS account
- **Spark events**: Hyperion API filtering spark stacking actions for the target EOS account
- **Yield/hour per property**: `yield_per_hour` field already in Upland API response

---

## UI

Lives at `/portfolio` in the web app.

```
┌─────────────────────────────────────────────────────────┐
│  Username: [pugs08]   [Analyze]                         │
└─────────────────────────────────────────────────────────┘

┌── Portfolio Summary ──────────────────────────────────────┐
│  82 properties  |  252,525 UPX mint value  |  61% built   │
│  Yield: 1,840 UPX/day across 12 neighborhoods             │
└───────────────────────────────────────────────────────────┘

Tabs: [ Overview ] [ Yield Efficiency ] [ Neighborhoods ] [ Spark ] [ Income History ]
```

---

## Dependencies

- Blockchain ownership cache (already built)
- Upland API property + structure data (already fetched in neighborhood optimizer)
- Economy scraper — for income history and yield event tracking
- Upland API auth credentials (for spark and yield event lookups)

---

## Related Tools

- Neighborhood Optimizer (PROJECT_PLAN.md) — links from neighborhood breakdown
- Collection Tracker (COLLECTION_TRACKER_PLAN.md) — portfolio view can surface collection completion %
- Property Valuation Tool (PROPERTY_VALUATION_PLAN.md) — for estimating current market value of portfolio
