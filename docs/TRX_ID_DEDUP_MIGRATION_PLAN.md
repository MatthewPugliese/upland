# `transactions` Table Dedup Key Migration — Plan (not yet executed)

## Status (2026-08-16)

**Scoped, not executed.** This doc is the plan for fixing the `trx_id` UNIQUE data-loss bug
found during the 2026-08-16 codebase review (see `project_session_2026_08_16.md` memory). Written
at the user's request ("scope out the plan but don't execute yet") — do not run any of the steps
below without explicit go-ahead, since it touches the live, actively-written production database.

---

## The bug, restated precisely

`transactions.trx_id` is `UNIQUE NOT NULL`. The scraper inserts with `INSERT OR IGNORE`. Hyperion
and greymass both return **one row per action**, not one row per transaction — so if a single
on-chain transaction bundles two or more of our tracked actions (e.g. two `n112` NFT sales batched
together), only the first one the scraper sees gets stored; the second silently vanishes. No error,
no log line.

## Measured impact (read-only check against live chain data, done 2026-08-16)

Sampled ~20,000 recent tracked actions (~4.5–9 days of AppChain activity) directly from Hyperion,
grouped by `trx_id`, independent of what's already in `economy.db`:

- **Bundling rate:** ~0.1% of transactions had 2+ tracked actions.
- Of those, **`n2`+`n2` and `n4`+`n4` bundles don't actually trigger this bug** — listing/unlisting
  actions write to `pending_usd_listings` keyed by `property_id`, not into `transactions` at all.
- **Real data loss is concentrated in `n112` (NFT sales)** — roughly 8–10 lost rows per sample
  window — **with an occasional `n5` (UPX property sale) also affected** (0–2 per sample).
- Loss rate among actual sale-type actions (n5/n52/n111/n112): **roughly 0.1%**.

Conclusion at the time: real but low-severity — worth fixing, not an emergency. This plan exists so
the fix is ready to execute whenever it's prioritized.

---

## The fix: capture `global_sequence` as the real dedup key

Verified directly against both APIs (raw JSON inspected 2026-08-16):

- **Hyperion** (`chain-history.upland.me` for AppChain, `eos.hyperion.eosrio.io` for EOS) returns
  `global_sequence` as a **top-level field** on every action (also duplicated inside
  `receipts[0].global_sequence`) — e.g. `"global_sequence": 315535901`.
- **greymass v1** (`eos.greymass.com`, used only by `greymass_backfill.py` for pre-2023 legacy data)
  returns the same concept as **`global_action_seq`** at the top level (also duplicated at
  `action_trace.receipt.global_sequence`).

`global_sequence` is a chain-wide, monotonically increasing, genuinely unique-per-action identifier
— exactly the right dedup key. It's already present in every API response the scraper receives
today; it's just not being captured or stored. This means the fix needs **no new data source**,
just start reading a field that's already there.

Confirmed via `grep -rn "trx_id" webapp/*.py` → **zero results**. No webapp/optimizer code assumes
"one row per trx_id" anywhere, so this migration has no downstream query-logic ripple effect —
only the scraper's own insert path and the table schema change.

---

## New schema

```sql
CREATE TABLE transactions_new (
    id           INTEGER PRIMARY KEY,
    trx_id       TEXT NOT NULL,              -- no longer UNIQUE alone
    global_seq   INTEGER UNIQUE,             -- NEW real dedup key. NULL allowed (SQLite permits
                                              -- unlimited NULLs under UNIQUE) — every pre-migration
                                              -- historical row gets NULL here, only new rows get a
                                              -- real value, so old data is untouched and unaffected.
    block_num    INTEGER,
    timestamp    TEXT NOT NULL,
    action       TEXT NOT NULL,
    property_id  TEXT,
    address      TEXT,
    city         TEXT,
    neighborhood TEXT,
    buyer        TEXT,
    seller       TEXT,
    upx_amount   REAL,
    usd_amount   REAL,
    marketplace  TEXT NOT NULL,
    asset_type   TEXT NOT NULL
);
CREATE INDEX idx_tx_ts     ON transactions_new(timestamp);
CREATE INDEX idx_tx_city   ON transactions_new(city);
CREATE INDEX idx_tx_action ON transactions_new(action);
CREATE INDEX idx_tx_prop   ON transactions_new(property_id);
CREATE INDEX idx_tx_trx    ON transactions_new(trx_id);   -- was implicit via the old UNIQUE index
```

SQLite can't `ALTER TABLE ... DROP CONSTRAINT` — changing a UNIQUE constraint requires the standard
12-step pattern: create the new table, copy data, drop the old table, rename. At 3,279,682 rows /
~1.1GB (current Pi DB size, checked 2026-08-16), this is a bulk `INSERT INTO ... SELECT`, not a
per-row operation — expected to take on the order of tens of seconds to a couple of minutes on the
Pi's hardware, not hours. Needs roughly 2x the table's disk space temporarily (old + new coexist
until the DROP); Pi had 95GB free as of this writing, so no concern there.

---

## Code changes needed

**`scraper/economy_scraper.py`** (`process_actions()`):
- Extract `global_seq = action.get("global_sequence")` alongside the existing `trx_id`/`timestamp`/
  `block_num` extraction (same flat top-level shape already being read from).
- Add `global_seq` to the column list and values tuple of all four `INSERT OR IGNORE INTO
  transactions` statements (n5, n52, n111, n112 — the only four that write to this table; n2/n4
  don't and need no change).

**`scraper/greymass_backfill.py`** (`process_page()`):
- Extract `global_seq = raw.get("global_action_seq")` (top-level, confirmed present in the
  greymass v1 payload shape) — same four INSERT statements updated the same way.

**Edge case to guard explicitly:** if `global_seq` is ever `None` (shouldn't happen per the two
verified API samples, but the code should not assume it always will), an `INSERT OR IGNORE` with a
NULL `global_seq` will never collide with anything (multiple NULLs are allowed under `UNIQUE`) —
meaning that specific row would lose dedup protection and could double-insert on a re-fetch/overlap.
Add a log line (not a hard failure) if `global_seq` comes back `None`, so this would actually be
noticed rather than silently degrading protection for that action.

---

## Testing plan (before touching production)

1. Pull a fresh consistent snapshot of the live `economy.db` via SQLite's online backup API (same
   technique already used earlier this session for the Property Valuation Tool's dev data) to a
   scratch location — never test against the only copy.
2. Run the migration SQL against that **copy**. Verify:
   - Row count identical before/after (`SELECT count(*)` — the migration only adds a column and
     changes a constraint, never touches existing row data).
   - `global_seq` is `NULL` for every pre-existing row, schema matches the new definition above.
   - A couple of known query patterns (e.g. the Valuation Tool's comp-search JOIN) run at
     comparable speed — confirms the recreated indexes are actually being used.
3. Update `economy_scraper.py` / `greymass_backfill.py`, run them briefly against the **migrated
   copy** (a few live-poll cycles, or a small backfill chunk) and confirm:
   - New rows get a real, non-null `global_seq`.
   - A deliberately re-processed/duplicate action is still correctly ignored (idempotency intact).
   - A synthetic bundled-action test (two crafted rows sharing one `trx_id` but different
     `global_seq`) — both insert successfully. This is the actual bug being fixed; write an
     explicit test for it rather than trusting the schema change alone.
4. Only after all of the above pass locally, schedule the production run.

## Production deployment sequence (when approved)

1. `free -h` / `df -h` on the Pi first (standing practice before touching anything there).
2. Stop both writers: `docker compose stop scraper` (Docker, clean) and gracefully `kill` the
   `greymass_backfill.py` bare process (SIGTERM — its existing signal handler already saves a
   precise resume position, confirmed working during today's deploy). Brief downtime is harmless;
   both are fully resumable.
3. `PRAGMA wal_checkpoint(TRUNCATE);` to flush the WAL cleanly, then take a **second** backup of
   `economy.db` at this exact point (belt-and-suspenders, on top of whatever backup already exists)
   — keep it until the migration is confirmed stable, don't delete it same-day.
4. Run the migration against the live file (safe now — no active writers).
5. Sanity-check row count and schema immediately after.
6. Deploy the updated scraper code (git pull, rebuild the lightweight `Dockerfile.scraper` image
   directly on the Pi — same safe pattern as every prior scraper deploy — restart), and restart the
   `greymass_backfill.py` bare process with the updated code.
7. Monitor: confirm new rows carry a real `global_seq`; watch scraper logs for a bit; re-check
   `/economy`, `/valuation`, `/floor` still work; RAM/disk still healthy.
8. Update `project_pi_state.md` / this doc with the outcome and date.

## Rollback plan

If anything looks wrong after step 4 but before confidence is established: restore the pre-migration
backup file over the live `economy.db`, revert the scraper code to the previous commit, restart both
writers. The backup file is the safety net — don't delete it until several days of healthy
post-migration operation have passed.

---

## Explicitly out of scope for this plan

**Recovering already-lost historical rows** (the ~0.1% already dropped over the dataset's
multi-year history) is a separate, much bigger undertaking — it would mean re-scanning the entire
chain history end-to-end again (comparable in cost to the original EOS/AppChain/greymass backfills,
one of which — the pre-2023 greymass backfill — is *still running today*, months in) just to find
and backfill a small trickle of missed rows. Given the low measured impact, this plan deliberately
does not attempt it. If ever wanted, it would be a distinct follow-up project, not part of this fix.
