# Portfolio Analyzer — Plan

## Goal

Given any Upland username, produce a full breakdown of their portfolio: properties owned, yield income, structures built, neighborhoods active in, and overall efficiency. Useful for analyzing your own portfolio and scouting other players.

---

## Features

### Your own portfolio view (`/portfolio?user=pugs08`)

- [ ] **Portfolio summary**
  - Total properties owned, total mint value, total current market value (estimated from comps)
  - Total yield per hour, yield per day, yield per month
  - Total UP² owned, % developed (has at least one structure)
  - Net worth estimate: mint value + structure replacement cost

- [ ] **Yield efficiency ranking**
  - For each property: yield/hour, mint price, yield/hour per UPX invested
  - Rank from best to worst ROI — shows which properties are underperforming
  - Flag properties with 0 yield (unlisted and unbuilt — dead weight)

- [ ] **Neighborhood breakdown**
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

- [ ] **Structure inventory**
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
