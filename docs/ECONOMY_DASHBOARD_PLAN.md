# Upland Economy Dashboard — Plan

## Goal

Build a real-time dashboard that tracks player-to-player marketplace activity across Upland's two distinct economies:

- **UPX Marketplace:** Property and asset trades settled in UPX tokens
- **USD Marketplace:** Property and asset trades settled in real US dollars

Inspired by the monthly Upland Discord announcement format ("500M UPX traded, $139K USD in sales") — this dashboard makes that data live, persistent, and explorable.

---

## Blockchain Findings (Verified Live)

> All of this was confirmed by directly querying the Hyperion API at `chain-history.upland.me`.

### Action codes that matter for the dashboard

| Action | Meaning | Key Fields | Currency |
|---|---|---|---|
| `n5` | P2P **property** sale — UPX marketplace | `p14`=buyer, `p24`=price (e.g. `"48000.00 UPX"`) | UPX |
| `n52` | P2P **property** sale — USD marketplace | `p14`=buyer, `a45`=property_id | **none on-chain** |
| `n111` | P2P **asset** (spark/equipment) sale — UPX | `p1`=buyer, `p2`=seller, `p45`=price | UPX |
| `n112` | P2P **NFT/dGoods** sale — UPX | `p14`=buyer, `p25`=seller, `p141`=price | UPX |
| `a4` | Initial property **mint** (Upland → player) | `a54`=player, `p44`=price | UPX (to communityupx) |
| `n2` | List property for sale | `a54`=seller, `a45`=property_id, `p11`=UPX price, `p3`=FIAT price | mutually exclusive |
| `n4` | Unlist property | `a54`=owner, `a45`=property_id | — |
| `n31` | Player collects yield earnings | `a54`=player, `p55`=property list | UPX (from communityupx) |

### The critical USD price problem

`n52` transactions contain **no price**. The USD marketplace processes real-money payments off-chain. The on-chain `n52` record is just a notarization of ownership change — `p14` (buyer) + `a45` (property_id), nothing else. The transaction has only 1 action total; no token transfers.

To recover the USD price, cross-reference with the preceding `n2` listing for that `property_id` where `p3` (FIAT price) is non-zero. The scraper must maintain a **pending USD listings cache**: `property_id → (seller, fiat_price_usd)` that it updates as `n2` events arrive and clears when an `n52` or `n4` fires.

### What `listings.py` gets wrong today

The existing code handles `n5` with a `"FIAT"` case and does `1 USD = 1,000 UPX` conversion — that code path is dead. In 4,000+ `n5` events scanned across 7 days, exactly **0** had a FIAT price. All `n5` prices are UPX. The FIAT sale action is `n52`, not `n5`.

### Observed transaction rates

| Action | Rate |
|---|---|
| `n5` UPX property sales | ~480 / day |
| `n52` USD property sales | ~170 / day |
| `n111`/`n112` asset sales | ~50 / day (rough) |
| `n2` listings | ~2,400 / day |

---

## Status

### Phase 1 — Data Pipeline ✅ COMPLETE (merged PR #8)

| Component | File | Status |
|---|---|---|
| Economy scraper | `scraper/economy_scraper.py` | **Done** — backfill + live daemon |
| SQLite DB | `data/economy.db` | **Done** — created on first run |
| Docker Compose | `docker-compose.yml` | **Done** — webapp + scraper as two services |
| Property lookup cache | `scraper/property_cache.json.gz` | **Done** — 4.7M properties |
| API credentials | `.env` | **Done** — Upland Developers API auth wired |

### Scraper: Running on Pi ✅

The scraper container (`upland-scraper-1`) is running on the Pi with `restart: unless-stopped`.
It auto-restarted after the Pi power cycle. Backfill is in progress.

```bash
# Check scraper status:
ssh -p 8008 -i ~/Desktop/rpi root@69.113.229.61
docker compose -f /opt/upland/docker-compose.yml logs -f scraper
```

### Webapp: NOT deployed to Pi yet ⚠️

The main `Dockerfile` builds gcc + libgeos (for Shapely) and **will OOM the Pi** the same way the previous attempt did. Do NOT run `docker compose build` on the Pi for the webapp service.

**Recommended approach: build on Mac, transfer to Pi**

Option A — Docker Hub (cleanest):
```bash
# On Mac:
docker build -t <dockerhub-user>/upland-webapp:latest .
docker push <dockerhub-user>/upland-webapp:latest
# On Pi:
docker pull <dockerhub-user>/upland-webapp:latest
docker compose up -d webapp
```

Option B — `docker save` / `scp` (no registry needed):
```bash
# On Mac:
docker build --platform linux/arm64 -t upland-webapp .
docker save upland-webapp | gzip > /tmp/upland-webapp.tar.gz
scp -P 8008 -i ~/Desktop/rpi /tmp/upland-webapp.tar.gz root@69.113.229.61:/tmp/
# On Pi:
docker load < /tmp/upland-webapp.tar.gz
docker compose up -d webapp
```

### Phase 2–4 — Not started

---

## Two Marketplaces, Two Independent Counters

Track separately, never convert between them:

| Counter | Source | Unit |
|---|---|---|
| UPX volume | Sum of `p24` from `n5` + `p45` from `n111` + `p141` from `n112` | UPX |
| USD volume | Price from prior `n2` FIAT listing, matched via `a45` on `n52` | USD |
| UPX trade count | Count of `n5` + `n111` + `n112` | trades |
| USD trade count | Count of `n52` events with recoverable price | trades |

---

## Architecture

```
Upland AppChain (Hyperion API)
        │
        ▼
  economy_scraper.py
  ┌────────────────────────────────────────────┐
  │  Poll n2, n4, n5, n52, n111, n112          │
  │  Maintain pending_usd_listings cache       │
  │    • n2 (FIAT) → store {prop_id: price}   │
  │    • n52 fires → look up price, record     │
  │    • n4 fires → remove from cache         │
  └────────────────────────────────────────────┘
        │
        ▼
  SQLite (single file, zero-config)
  ┌─────────────────────────────────────────┐
  │  transactions                           │
  │  pending_usd_listings                   │
  │  hourly_aggregates                      │
  └─────────────────────────────────────────┘
        │
        ▼
  Flask API  (new routes in webapp/app.py)
  /api/economy/summary?period=
  /api/economy/timeseries?period=&interval=
  /api/economy/feed
  /api/economy/stream  (Server-Sent Events)
        │
        ▼
  /economy page  (new Jinja2 template)
  ┌────────────────────────────────────┐
  │  UPX total  |  USD total           │
  │  Volume chart (dual axis)          │
  │  Live transaction feed             │
  │  City breakdown table              │
  └────────────────────────────────────┘
```

---

## Database Schema

### `transactions`
| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | auto |
| `trx_id` | TEXT UNIQUE | blockchain tx hash, dedup key |
| `block_num` | INTEGER | |
| `timestamp` | DATETIME | UTC, indexed |
| `action` | TEXT | `n5`, `n52`, `n111`, `n112` |
| `property_id` | TEXT | nullable for asset sales |
| `city` | TEXT | from property_cache lookup |
| `neighborhood` | TEXT | from property_cache lookup |
| `buyer` | TEXT | EOS account |
| `seller` | TEXT | EOS account (from prior `n2` for `n52` sales) |
| `upx_amount` | REAL | NULL for USD sales |
| `usd_amount` | REAL | NULL for UPX sales |
| `marketplace` | TEXT | `"upx"` or `"usd"` |
| `asset_type` | TEXT | `"property"` or `"asset"` |

### `pending_usd_listings`
| Column | Type | Notes |
|---|---|---|
| `property_id` | TEXT PK | |
| `seller` | TEXT | EOS account |
| `usd_price` | REAL | FIAT price from `n2` |
| `listed_at` | DATETIME | for expiry/debugging |

This staging table is the bridge: `n2` FIAT listings write here, `n52` reads+deletes here, `n4` deletes here.

### `hourly_aggregates`
| Column | Type | Notes |
|---|---|---|
| `hour` | DATETIME | |
| `marketplace` | TEXT | `"upx"` or `"usd"` |
| `trade_count` | INTEGER | |
| `volume` | REAL | UPX or USD depending on marketplace |

Compound PK on `(hour, marketplace)`. Rebuilt from `transactions` on startup, incremented live.

---

## Scraper Worker (`economy_scraper.py`)

New file. Does NOT extend `listings.py` directly — instead imports the chain/API functions and builds a clean scrape loop optimized for persistence.

### Poll loop (runs every 5s)

1. Call Hyperion for `playuplandme` actions since `last_timestamp`, limit 100
2. For each action in order:
   - `n2` with FIAT price → upsert into `pending_usd_listings`
   - `n4` → delete from `pending_usd_listings`
   - `n5` → insert transaction with UPX price from `p24`; seller from UPX transfers in same tx
   - `n52` → look up property in `pending_usd_listings` for USD price; insert transaction; delete from pending
   - `n111` → insert transaction with UPX price from `p45`
   - `n112` → insert transaction with UPX price from `p141`
3. Update `hourly_aggregates`
4. Advance `last_timestamp`

### Seller resolution for `n52`

`n52` doesn't include the seller EOS account. Options in priority order:
1. Use `pending_usd_listings.seller` (set when `n2` was seen — reliable if scraper has been running)
2. Fall back: scan recent `n2` events for this `property_id` via Hyperion (for backfill/restart cases)
3. If unresolvable, record `seller = NULL` and `usd_amount = NULL` — still count the trade, just without full metadata

### Backfill on first run

The scraper walks both chains automatically in one run:
- **EOS mainchain** (`eos.hyperion.eosrio.io`): Mar 18, 2023 → Apr 28, 2025
- **Upland AppChain** (`chain-history.upland.me`): Apr 28, 2025 → present

Timestamp-based pagination bypasses Hyperion's 10k skip cap. Cursor stored in `scraper_state` table — safe to restart mid-backfill.

Run modes:
```bash
python3 scraper/economy_scraper.py               # backfill then live daemon
python3 scraper/economy_scraper.py --backfill-only
python3 scraper/economy_scraper.py --live-only
python3 scraper/economy_scraper.py --stats
```

---

## Flask API Endpoints

All new, added to `webapp/app.py`:

| Endpoint | Returns |
|---|---|
| `GET /api/economy/summary?period=month` | `{ upx_volume, usd_volume, upx_trades, usd_trades, period_start }` |
| `GET /api/economy/timeseries?period=30d&interval=1h` | Array of `{ timestamp, upx_volume, usd_volume }` |
| `GET /api/economy/feed?limit=50&marketplace=upx` | Latest sales with address/city |
| `GET /api/economy/cities?period=30d` | Per-city breakdown |
| `GET /economy/stream` | SSE — pushes new transactions as JSON events |

Period options: `today`, `7d`, `30d`, `90d`, `all`

---

## Dashboard UI (`/economy`)

New page extending the existing Flask template system.

### Layout

**1. Hero Totals**
```
┌─── UPX Marketplace ───┐   ┌─── USD Marketplace ───┐
│   500,000,000 UPX     │   │     $139,000 USD       │
│   48,203 trades       │   │      2,841 trades      │
└───────────────────────┘   └───────────────────────┘
          [ Today | 7 Days | 30 Days | All Time ]
```

**2. Volume Over Time**
Dual-axis line chart (Chart.js):
- Left y-axis: UPX volume (in millions)
- Right y-axis: USD volume
- Auto-resolution: 1-hour buckets for ≤7d view, 1-day buckets for 30d+

**3. Live Transaction Feed**
Auto-updates via SSE (10-second batch push to avoid noise):
```
just now   481 RUE DU COMMERCE, Paris       buyer42 → seller7   12,500 UPX
1 min ago  789 Oak Ave, Nashville            buyer11 → seller2      $45 USD
3 min ago  (asset)  Spark unit              buyer88 → seller99   4,200 UPX
```
Filter pills: All | UPX only | USD only

**4. City Breakdown**
Sortable table: City | UPX Volume | USD Volume | Total Trades | Avg UPX Price | Avg USD Price

---

## Deployment

The scraper runs as a second process alongside Flask.

**Docker Compose (two services, shared volume) — already wired in `docker-compose.yml`:**
```yaml
services:
  webapp:
    build: .                         # uses main Dockerfile (has gcc/libgeos for Shapely)
    ports: ["8080:5000"]
    volumes:
      - ./data/cache:/app/webapp/cache
      - ./data/maps:/app/webapp/maps
      - ./data:/app/data
    environment:
      - ECONOMY_DB=/app/data/economy.db
    restart: unless-stopped
    deploy:
      resources:
        limits: {cpus: "1.0"}

  scraper:
    build:
      context: .
      dockerfile: Dockerfile.scraper  # lightweight: python:slim + requests only, no gcc
    command: python /app/scraper/economy_scraper.py
    volumes:
      - ./data:/app/data
      - ./scraper/property_cache.json.gz:/app/scraper/property_cache.json.gz:ro
    environment:
      - ECONOMY_DB=/app/data/economy.db
    restart: unless-stopped
    deploy:
      resources:
        limits: {cpus: "0.5"}         # leaves headroom for Home Assistant + Son of Adam
```

> **Pi note:** The original `Dockerfile` pulled gcc/libgeos for Shapely and OOM'd the Pi during build (~1.8 GB RAM, HA + Son of Adam running). `Dockerfile.scraper` is ~30s build, ~60 MB image — safe. Merged in PR #19.

SQLite with WAL mode handles one writer (scraper) + multiple readers (Flask) safely.

**Target host: Raspberry Pi** — `docker compose up -d` keeps both services running permanently with automatic restart on reboot.

---

## Remaining Unknowns (Small)

1. **USD asset sales.** `n112` has a `p143` field that is always `null` in observed data — it may be the FIAT price for USD-marketplace asset sales. No `n113` action exists. The Upland Discord announcement says "properties and player-owned assets" — if player-owned asset USD sales exist on-chain, they likely show up in `n112` with `p143` non-null. Low priority to verify; property sales dominate volume.

2. **Seller in `n52` during backfill.** For historical `n52` events the scraper wasn't alive for, we can query Hyperion backwards for the property's prior `n2` to get the FIAT price. Worth implementing but not blocking.

3. **SQLite ceiling.** At ~700 trades/day, 90-day history = ~63k rows. SQLite is more than sufficient for years. Postgres only needed if we add high-frequency event logging beyond sales.

---

## Additional Feature Ideas

- [ ] **Neighborhood price heatmap**
  - After building the city breakdown table, add a choropleth map layer: color each neighborhood by average sale price (UPX or USD) over the selected period
  - Instantly shows which neighborhoods are hot vs depressed
  - Data already available from `transactions` table joined to property_cache

- [ ] **Floor price tracker**
  - For each neighborhood, track the lowest current active listing (from `n2` events in `pending_usd_listings` + open UPX listings)
  - Show: neighborhood | floor price UPX | floor price USD | # properties listed | last updated
  - Update live as `n2`/`n4` events arrive

- [ ] **Days-on-market analysis**
  - Compute time between `n2` (listed) and `n5`/`n52` (sold) or `n4` (unlisted) for each property
  - Show: avg days on market per city, per price range
  - Flag listings that have been sitting unusually long — potentially overpriced

- [ ] **Whale tracker** ⚠️ blocked on username resolution
  - Backend query done (`/api/economy/whales`) — top buyers/sellers by volume
  - UI built and then removed — EOS account names (e.g. `qbi3jazhmbrh`) are not the same as Upland display names; no public API to resolve them; Developers API user endpoints return 403 with current credentials
  - Profile URL format: `https://play.upland.me/profile?username={display_name}` — but display name ≠ EOS account
  - Re-enable once username resolution is figured out (API tier upgrade, community-sourced mapping, or scraping profile pages)

- [ ] **Mint activity tracker**
  - Track `a4` (mint) events: how many new properties minted per day, per city
  - Overlaid on the volume chart: shows whether market activity is driven by new supply or secondary trading
  - Flag cities with high mint rates — early-minted neighborhoods often have price pressure

- [ ] **Price fairness estimator**
  - Given a property ID or address, pull recent `n5`/`n52` sales for nearby properties (same neighborhood, similar UP²) from the DB
  - Show: "properties of this size in Dongan Hills sold for 8,200–14,500 UPX in the last 30 days — this listing at 11,000 UPX is within range"
  - Ties into the Property Valuation Tool (see PROPERTY_VALUATION_PLAN.md)

---

## Phased Build Order

### Phase 1 — Data Pipeline ✅ DONE
- [x] Create SQLite schema (`transactions`, `pending_usd_listings`, `hourly_aggregates`, `scraper_state`)
- [x] Write `scraper/economy_scraper.py` — poll loop for `n2`, `n4`, `n5`, `n52`, `n111`, `n112`
- [x] EOS mainchain backfill (Mar 2023–Apr 2025) via `eos.hyperion.eosrio.io`
- [x] AppChain backfill (Apr 2025–present) via `chain-history.upland.me`
- [x] USD price recovery via `pending_usd_listings` staging table + reverse lookup fallback
- [x] Timestamp cursor pagination (bypasses Hyperion 10k skip cap)
- [x] Docker Compose two-service setup (webapp + scraper, shared `./data` volume)
- [x] **Run backfill on Pi** — complete. EOS chain Mar 2023–Apr 2025 (829K rows) + AppChain Apr 2025–Jun 2026 (~150K rows). Now live polling every 5s.

### Phase 2 — API ✅ DONE
- [x] Add `/api/economy/*` routes to `webapp/app.py`
- [x] Test summary, timeseries, and feed endpoints manually
- [x] `/api/economy/summary`, `/api/economy/timeseries`, `/api/economy/feed`, `/api/economy/cities`

### Phase 3 — Dashboard ✅ DONE
- [x] Hero totals with period selector (today / 7d / 30d / all)
- [x] Chart.js volume chart (dual axis: UPX left, USD right)
- [x] Live transaction feed with 30s JS polling and marketplace filter
- [x] Webapp deployed to Pi at `http://192.168.1.115:8080/economy` (LAN only — port 8080 not forwarded externally)

### Phase 4 — Polish
- [x] City/neighborhood breakdown table (sortable)
- [ ] Asset type filtering (property vs spark/equipment)
- [ ] Neighborhood price heatmap overlay on existing map

### Known issues to fix
- [ ] **30d/7d/today show low data** — scraper just caught up to present on 2026-06-24; data will accumulate naturally over the coming days/weeks
- [ ] **Dashboard page spins intermittently** — root cause unclear; possibly SQLite read contention with active scraper writes, or lingering iptables weirdness from OOM events. Needs investigation.

### Pending — backfill UPX sellers
UPX sales (n5) historically have `seller = NULL` because the seller isn't in the n5 action data — it's in a `upxtokenacct::transfer` inline action in the same transaction. Fix requires fetching full transaction via `get_transaction?id={trx_id}` for each n5 row with null seller.

- Live scraper now captures seller correctly going forward (`fetch_n5_seller()`)
- ~695K historical n5 rows have null seller — need a backfill script similar to `backfill_addresses.py`
- At 5 req/s that's ~39h; run overnight on Pi after address backfill completes

### Pending — sync local DB improvements back to Pi
Local DB (`/tmp/economy_live.db`) is ahead of Pi in two ways:
1. **Address backfill** — `backfill_addresses.py` is filling in 192K missing address/city/neighborhood rows (running locally, ~11h). Once complete, copy the updated DB to Pi.
2. **Code changes** — city filter, property-only feed, live API address lookup — need a new webapp image built and deployed.

**Sync steps (when backfill completes):**
```bash
# 1. Stop Pi scraper so it's not writing during transfer
ssh -p 8008 -i ~/Desktop/rpi root@69.113.229.61 "docker stop upland-scraper-1"

# 2. Checkpoint local DB and copy to Pi
python3 -c "import sqlite3; sqlite3.connect('/tmp/economy_live.db').execute('PRAGMA wal_checkpoint(FULL)')"
scp -P 8008 -i ~/Desktop/rpi /tmp/economy_live.db root@69.113.229.61:/opt/upland/data/economy.db

# 3. Build and deploy updated webapp image
docker build --platform linux/arm64 -f Dockerfile.webapp -t upland-webapp . && \
  docker save upland-webapp | gzip | ssh -p 8008 -i ~/Desktop/rpi root@69.113.229.61 "gunzip | docker load"

# 4. Restart both services on Pi
ssh -p 8008 -i ~/Desktop/rpi root@69.113.229.61 \
  "docker compose -f /opt/upland/docker-compose.yml up -d"
```

### Deployment notes
- Webapp uses `Dockerfile.webapp` (light: flask + gunicorn + requests only, ~160MB image, no shapely/matplotlib/numpy)
- Map/optimizer routes lazy-load heavy deps — will error if hit on Pi since those packages aren't installed; run those features from Mac
- Pi kernel does not support cgroup memory limits — `memory limit capabilities` warning on startup is harmless
- Pi swap expanded from 200MB → 1GB (`/etc/dphys-swapfile`) to give headroom alongside HA
- HA uses ~1.5GB RAM after warmup; with 1GB swap the system is stable at idle
- Webapp image must be built on Mac (`--platform linux/arm64`) and transferred via `docker save | gzip | ssh ... gunzip | docker load`
