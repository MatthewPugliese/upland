# Collection Tracker — Plan

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

- [ ] **Collection membership scan**
  - For every collection in the game, check how many of its required properties you own
  - Show: collection name | category | boost | required | you own | missing | % complete

- [ ] **Completion priority ranking**
  - Rank collections by cost-to-complete: sum of cheapest available listing prices for all missing properties
  - Also rank by yield impact: how much does completing this collection increase your total yield/hour
  - Flag collections where you own all but 1–2 properties — these are the easiest wins

- [ ] **Cheapest missing properties**
  - For each target collection, list the missing properties with their current listing price (if on market)
  - Flag which ones are unlisted (harder to acquire — need to make an offer)
  - Show last known sale price for unlisted properties (from economy scraper) as a negotiation anchor

- [ ] **Yield impact calculator**
  - Current total yield/hour across all your properties
  - Projected yield/hour after completing collection X
  - Payback period: how many hours of yield does the acquisition cost represent

- [ ] **Collection map**
  - For collections where all properties are in one geographic area (neighborhood or city collections), show them on a map
  - Color: owned (green), missing + listed (yellow), missing + unlisted (red)

- [ ] **Multi-collection optimizer**
  - Given a UPX budget, find the combination of collection completions that maximizes yield/hour gain
  - E.g., "With 50,000 UPX you can complete Collection A (+12% yield) or collections B+C (+8% + +6%) — B+C wins"

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
