# For-Sale Snapshot Scraper — Plan

## Goal

Maintain a local, queryable snapshot of "what's for sale right now, at what price, vs. mint" —
so questions like "what's listed at the lowest markup" or "cheapest way to complete this
collection" are a SQL query against fresh-ish local data, not a live multi-hour API crawl.

## Status (2026-11-09)

**Phase 1 crawler is in the repo. It has not been run to completion across the owned cities.**

Shipped:

- `scraper/listings_db.py` — `data/listings.db` (separate from `economy.db`), upsert that keeps a stored price when a price lookup fails, and stale-row deletion scoped to one finished city or one finished neighborhood.
- `scraper/listings_snapshot.py` — resumable crawler. Full page scan only when a city fits in the developers API window (pageSize 100, currentPage max 99, so 9,900 properties). Larger owned cities are deferred; each neighborhood the account owns there is crawled by street `textSearch` instead. A partial or failed scan does not delete unseen listings.
- `/listings` — browse the snapshot (lowest UPX markup by default).
- Collection for-sale finder uses a finished city or neighborhood crawl from the last 36 hours as coverage. An empty result then means nothing matching is listed. When the snapshot is only partial, the finder still shows the stored matches and says it is not a full city scan. It does not call Upland for a neighborhood or street requirement the snapshot can name. A blank partial result is not stored as finished coverage.

Decisions that were open in the design below:

- Separate database: `data/listings.db`.
- Listings that disappear are deleted, and only after the scope that contained them finished completely.
- The 50k-property cutoff in the original design is tighter in code: 9,900, because the API will not serve a page past 99. `--include-large` pages up to that cap and stores the target as partial. It does not claim the city was covered.

Still open: run the owned-city queue (smallest cities first), then time a real pass before picking a re-scan cadence. Pi deployment is unchanged from the design below and has not been done.

2026-11-09: a live page scan from this machine was blocked by Cloudflare on `api.prod.upland.me` (403 challenge page, before auth could matter). The public property endpoint still works. `listings_snapshot.py --reprice-caches` rechecked 359 for-sale ids already sitting in local property caches and stored 344 that are still listed (294 UPX, 50 USD). That target stays `partial`. The collection finder reads those rows and labels them as a partial snapshot. It does not treat the reprice as a finished city crawl, and it does not fall through to a live scan for a neighborhood or street requirement. Lowest UPX markups in that partial set were about +15% in Gardens, Cape Town.

---

## The Problem

Asked "what properties are listed at the lowest mint markup, across the board" (2026-08-24, no
neighborhood/collection scoping). No local data source answers this:

| Source | What it has | Why it doesn't help |
|---|---|---|
| `data/economy.db` `transactions` | Completed sales only | A sale that already happened isn't a current listing |
| `data/economy.db` `pending_usd_listings` | USD listing→sale matching helper | Narrow, transient, not a full snapshot |
| `scraper/property_cache.db` | address/city/neighborhood per property ID | No price or status field at all |

None of them capture live "for sale right now, at price X" state — that only exists on Upland's
servers. Answering the question required a live scan of the Developers API, which turned out to
be extremely expensive:

- **No server-side "for sale only" filter exists.** Tested `status`, `onMarket`, `forSale` as
  query params against `/properties?cityId=X&currentPage=Y&pageSize=100` — all silently ignored
  (`totalResults` and returned rows identical with or without them). Every property in a city has
  to be paged through and filtered client-side.
- **City sizes vary by 2 orders of magnitude.** Smallest owned-in city: Park City at 4,859
  properties. Largest: Los Angeles at 655,127, Chicago at 562,458, Detroit at 386,132, Queens at
  307,355. At `pageSize=100` that's 6,500+ paginated requests for LA alone.
- **The list endpoint doesn't include live price/currency** — only `mintPrice` and `status`. Every
  property found with `status == "For sale"` needs a *second* API call
  (`https://api.upland.me/properties/{id}`) to get `on_market.currency` + `price` before a markup
  can be computed.
- Scoped to just the user's 28 cities under 50k properties each (deliberately excluding LA,
  Chicago, Manhattan, Nashville, Kansas City, etc.), that's still **~625K properties / ~6,250+
  paginated list requests**, plus a price lookup for every "For sale" hit — estimated 30-90 minutes
  even with 8-way concurrency, and it's thrown away after answering one question.

The scan was started, then killed mid-run at the user's request (too slow for the immediate need).
This doc captures the design for doing it properly instead, as a standing background service.

---

## Design

### Data model

New SQLite table, `current_listings`, in a new `data/listings.db` (or added to `economy.db` — TBD,
lean toward separate file so a bug here can't touch the transaction-history data the rest of the
app depends on):

```sql
CREATE TABLE current_listings (
    property_id   TEXT PRIMARY KEY,
    address       TEXT,
    city          TEXT,
    neighborhood  TEXT,
    currency      TEXT,        -- 'UPX' or 'USD'
    price         REAL,
    mint_price    REAL,
    markup_pct    REAL,        -- 100 * (price - mint_price) / mint_price, UPX only (USD/UPX don't compare directly)
    owner_username TEXT,
    last_seen     TEXT NOT NULL   -- ISO timestamp of the crawl that confirmed this listing
);
CREATE INDEX idx_listings_markup ON current_listings(markup_pct);
CREATE INDEX idx_listings_city   ON current_listings(city);
CREATE INDEX idx_listings_neighborhood ON current_listings(neighborhood);
```

A property that stops being "For sale" on a later crawl gets deleted (or flagged, TBD — deletion
is simpler and the table is a live snapshot, not a history).

### Crawl strategy — prioritized, not exhaustive

Given the city-size problem above, a "recrawl every city fully, every cycle" design never
converges for LA/Chicago-scale cities. Instead, prioritize what the user's tools actually need:

1. **Phase 1 — user-relevant scope.** Crawl only:
   - Cities where the user owns at least one property (currently 44, per `load_user_properties`)
   - Neighborhoods tied to the user's near-complete collections (same set `forsale_finder.py`
     already targets)

   This directly serves the multi-collection budget optimizer and any "best markup" query scoped
   to the user's actual footprint, without trying to boil the ocean.

2. **Phase 2 — stretch.** Expand to full coverage of small/medium cities (<50k properties, ~28
   cities, ~625K properties) on a slow rotating cadence — think of it like `greymass_backfill.py`:
   a single long-running process that persists its position and crawls forward continuously,
   rather than a cron job that tries to finish in one pass.

   Cities over 50k (LA, Chicago, Detroit, Queens, Manhattan, Nashville, Kansas City, Fresno,
   Porto, Cleveland, Bakersfield, Bronx, Sao Paulo, Las Vegas, Staten Island, San Francisco) stay
   out of scope indefinitely unless a specific need justifies the cost — flag this explicitly to
   whoever's using the data rather than silently claiming full coverage.

### Deployment shape (mirrors `greymass_backfill.py`)

- Bare Python process on the Pi (not inside the scraper Docker container — keep the blast radius
  separate; the scraper is critical infrastructure, this is a nice-to-have), started manually,
  **not** systemd-managed initially (same as greymass backfill — acceptable since it's not
  critical, just needs a restart-after-reboot note in [[project_pi_state]] once it exists).
- Persists crawl position (which city/page it's on) so a restart resumes instead of starting over.
- Lower concurrency than the ad-hoc 8-worker Mac scan — the Pi is weaker (1.8GB RAM, no cgroup
  limits, shared with the live scraper + webapp + Home Assistant). Start at 2-3 concurrent
  requests, tune from observed RAM/CPU impact per [[feedback_check_ram_before_pi]].
- Respect the 409 backoff pattern already used everywhere else in this codebase
  (`neighborhood_map.upland_get`, `forsale_finder._search_city_by_street`) — same exponential
  backoff, don't invent a new retry scheme.
- Re-scan cadence: TBD, but listings and prices change continuously, so "last_seen" staleness of
  hours-to-a-day is probably fine for a markup-hunting tool — this isn't a real-time trading
  system. Revisit once Phase 1 is running and its actual full-cycle time is known.

### Integration points (once it exists)

- **Multi-collection budget optimizer** (`webapp/multi_collection_optimizer.py`,
  `webapp/forsale_finder.py`) — replace the live per-collection API scan with a query against
  `current_listings` filtered by neighborhood/street match. Should be dramatically faster and
  usable without the 30-min-TTL disk cache hack currently in `forsale_finder.py`.
- **Floor Price Tracker** (`webapp/floor_price.py`) — same substitution, live scan → table query.
- **Property Valuation Tool** (`docs/PROPERTY_VALUATION_PLAN.md`) — "current listing" comparison
  becomes a table lookup instead of a live API call per query.
- **New "lowest markup" feature** — whatever surface answers this question in the future (a new
  webapp route, or just an ad-hoc script) queries `current_listings` directly:
  `SELECT * FROM current_listings WHERE currency='UPX' ORDER BY markup_pct ASC LIMIT 20`.

---

## Open Questions

- Real API rate-limit ceiling is unknown — only observed behavior is sporadic 409s under existing
  moderate concurrency, handled by backoff. Phase 1's lower concurrency should be conservative
  until there's evidence it can go faster without errors climbing.
- Separate DB file (`data/listings.db`) vs. a new table in `economy.db` — decided: separate file.
- Whether removed-from-market properties should be deleted or soft-flagged (`still_listed=0`) —
  decided: delete, and only after a complete scan of that city or neighborhood. Revisit if
  anything wants listing history.
- Full Phase 2 cycle time for the 28 sub-50k cities (~625K properties) is unmeasured — the earlier
  live attempt was killed after only completing Park City. Once Phase 1 ships, time a real Phase 2
  pass before committing to a re-scan cadence.

---

## Related

- [[project_forsale_snapshot_scraper_idea]] — memory note this doc formalizes
- `docs/PROPERTY_VALUATION_PLAN.md`, `docs/COLLECTION_TRACKER_PLAN.md` — consumers once this ships
- [[project_pi_state]] — Pi deployment patterns and constraints this should follow
