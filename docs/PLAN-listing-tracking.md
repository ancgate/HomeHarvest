# Plan: track listings over time in HomeHarvest

> **Status: phases 1-6 are implemented** (`homeharvest/tracking/`, tests in `tests/test_offline_search_stats.py`,
> `tests/test_offline_tracking_store.py`, `tests/test_offline_tracking.py`). Phase 7, the live check, has to run on
> your machine. Where the implementation deliberately differs from this plan:
>
> - **Removals are decided once, at the end of a run, per area** (the whole location, or one ZIP with
>   `split_by="zip_code"`), and only if *every* chunk of that area finished completely. Per-chunk removal (as first
>   planned) reported listings that moved between listing types (for sale -> pending -> sold), or into a chunk that
>   failed, as removed. Tests cover both cases.
> - Sold listings that age out of the search are deactivated without a `removed` change.
> - **The command is `python -m homeharvest.tracking`** (also `homeharvest-track` after `pip install`), not
>   `python -m homeharvest.track`: a `homeharvest/track.py` module would shadow the `homeharvest.track()` function.
> - Also added: `get_runs()`, `map_changes()`, `TrackResult.chunks`, and `--map` / `--risk` / `--near` CLI options.

**Goal.** Run the same search again and again (for example weekly) and find out what changed since last time:
new listings, price cuts and increases, listings that went under contract, sold, came back on the market, or
disappeared. An interrupted run picks up where it stopped. The idea comes from the LandWatch scraper
(`ancgate/landharvest`: `landwatch/store.py`, `run.py`), adapted to HomeHarvest and Realtor.com.

**What you get when it's done**

```py
from homeharvest import track, get_changes, get_history

result = track(
    "lehigh-land",                       # name of this saved search ("watch")
    location="Lehigh Acres, FL",
    listing_type=["for_sale", "pending", "sold"],
    property_type=["land"],
    db="homeharvest.db",                 # SQLite file that remembers everything
)
print(result.summary)                    # {'new': 41, 'price_cut': 17, 'under_contract': 9, 'sold': 6, 'removed': 12, ...}
result.changes                           # DataFrame: one row per change this run
result.listings                          # DataFrame: every listing currently on the market for this watch

get_changes("homeharvest.db", "lehigh-land", since="2026-11-01")   # changes across runs
get_history("homeharvest.db", property_id="1234567890")           # price/status timeline of one property
```

```bash
python -m homeharvest.tracking lehigh-land --location "Lehigh Acres, FL" --listing-type for_sale pending sold --property-type land
```

---

## Before you start

1. **Merge the open branch first.** The five commits on `claude/busy-mendel-aavs1w` (polite requests, splitting
   past 10,000 results, OpenStreetMap distances, place types) are pushed but not in `master`. This plan relies on
   the polite client (`homeharvest/core/client.py`) and the splitting (`RealtorScraper._search_past_cap`).
2. Work on a new branch from `master`, one commit per phase below.
3. Every phase is tested offline with the fake API in `tests/fakes.py`. Realtor.com isn't reachable from Claude's
   cloud sessions, so do a short live run on your own machine at the end (phase 7).

## Key decisions (already made, change them here if you disagree)

| Question | Decision | Why |
|---|---|---|
| Storage | SQLite file, standard library `sqlite3`, WAL mode | No new dependency; same as the LandWatch scraper |
| Identity of a listing | `property_id` (stable per property); `listing_id` changing means it was relisted | A Realtor.com property keeps its `property_id` across listings |
| Unit of tracking | A named **watch** = a location + the `scrape_property` arguments | Changes only make sense against the same search |
| When is something "removed" | Only if the part of the run that covers it finished cleanly **and wasn't truncated** by `limit` or the 10,000 cap | Prevents a bad network day or a capped search from "removing" thousands of listings (same rule as LandWatch) |
| Resume granularity | A run is split into **chunks** (one per listing type, optionally per ZIP code); finished chunks are skipped on resume | `scrape_property` is one call; chunks give restart points |
| Under contract | Status becomes `PENDING` or `CONTINGENT` | That's how HomeHarvest reports it |
| Sold | Status becomes `SOLD` (record `sold_price`, `last_sold_date`) | |
| Prices compared | `list_price`; for sold listings `sold_price` | |
| Raw data kept | The flattened row as JSON per listing (latest) | Lets exports rebuild everything without re-scraping |

---

## Phase 1: give the scraper a "how complete was this search" report

Tracking must know whether a search returned everything, or it can't safely mark listings as removed.

**Change**
- `homeharvest/core/scrapers/realtor/__init__.py`: add `self.stats = {"reported_total": 0, "fetched": 0,
  "capped": False, "truncated_by_limit": False}` in `__init__` (shared by the `copy.copy` sub-scrapers, so make it
  a dict that copies keep pointing at). Fill it in `_search_core` (the `total` from the first page) and in
  `_search_past_cap` (set `capped=True` where it warns that a search can't be split further).
- `homeharvest/__init__.py`: move the body of `scrape_property` into `_scrape_property(..., _with_stats=False)`
  that returns `(results, stats)`; `scrape_property` keeps its current signature and return value.

**Done when**: a test with the fake API (cap patched to 10, as in `tests/test_offline_splitting.py`) shows
`capped=True` for an unsplittable off-market search and `False` otherwise, and `truncated_by_limit=True` when
`limit=5` and 25 listings match. All existing tests still pass.

## Phase 2: the store (`homeharvest/tracking/store.py`)

**Schema**

```sql
CREATE TABLE watches (
    name        TEXT PRIMARY KEY,
    params_json TEXT NOT NULL,          -- location + scrape_property kwargs (sorted JSON)
    created_at  TEXT NOT NULL
);
CREATE TABLE runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    watch       TEXT NOT NULL REFERENCES watches(name),
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,          -- running | done | partial | blocked | network_down | interrupted | failed
    stats_json  TEXT
);
CREATE TABLE run_chunks (
    run_id      INTEGER, chunk TEXT,    -- e.g. "for_sale", "sold|zip=33971"
    status      TEXT,                   -- done | failed
    reported_total INTEGER, fetched INTEGER, complete INTEGER,   -- complete = not capped, not truncated
    finished_at TEXT,
    PRIMARY KEY (run_id, chunk)
);
CREATE TABLE listings (
    watch       TEXT, property_id TEXT,
    listing_id  TEXT, chunk TEXT,
    status      TEXT, list_price REAL, sold_price REAL,
    first_seen  TEXT NOT NULL, last_seen TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1, removed_at TEXT,
    row_json    TEXT NOT NULL,          -- latest flattened DataFrame row
    PRIMARY KEY (watch, property_id)
);
CREATE TABLE observations (             -- one row each time price or status changed
    watch TEXT, property_id TEXT, observed_at TEXT, status TEXT, list_price REAL, sold_price REAL
);
CREATE TABLE changes (
    run_id INTEGER, watch TEXT, property_id TEXT,
    change_type TEXT,                   -- see phase 3
    old_value TEXT, new_value TEXT, amount REAL, pct REAL, at TEXT
);
CREATE INDEX idx_changes_watch ON changes(watch, at);
CREATE INDEX idx_obs_property ON observations(watch, property_id);
```

**Class**: `ListingStore(path)` with `get_or_create_watch(name, params)`, `start_run(watch)`,
`unfinished_run(watch, max_age_days=6)`, `finish_run(run_id, status, stats)`, `chunk_done(run_id, chunk)`,
`record_chunk(run_id, chunk, stats)`, `upsert_listings(run_id, watch, chunk, rows) -> list[change]`,
`mark_removed(run_id, watch, chunk, run_started) -> int`, `iter_active(watch)`, `changes(watch, since, run_id)`,
`history(watch, property_id)`. One lock around writes (threads), `PRAGMA journal_mode=WAL`.

- If a watch's saved params differ from the ones passed in, raise `ValueError` (changing the search would make
  every listing look new or removed). Offer `track(..., reset=True)` to start over.
- Store timestamps as UTC ISO strings.

**Done when**: unit tests create a store in `tmp_path`, insert rows twice and read back changes and history.

## Phase 3: change detection (in `upsert_listings` / `mark_removed`)

For each listing in a chunk's results, compare with the stored row:

| change_type | Rule |
|---|---|
| `new` | `property_id` not seen before in this watch |
| `price_cut` / `price_increase` | `list_price` changed (store `amount` and `pct`); ignore changes under $1 |
| `under_contract` | status moved from `FOR_SALE`/`FOR_RENT` to `PENDING` or `CONTINGENT` |
| `back_on_market` | status moved from `PENDING`/`CONTINGENT` back to `FOR_SALE` |
| `sold` | status became `SOLD` (`new_value` = sold price) |
| `status_change` | any other status change |
| `relisted` | listing was inactive (removed) and is seen again, or `listing_id` changed |
| `removed` | active listing not seen in a **complete** chunk that covers it (see the decision table) |

Write an `observations` row whenever price or status changed (and for `new`). After the run, a listing that was
`removed` and then shows up as `SOLD` later counts as `sold`, not `relisted`.

**Done when**: tests with the fake API run `track()` twice with edited fixture homes and get exactly the expected
change rows for each type, including: no `removed` when the second run is capped or `limit`-truncated, and no
`removed` for chunks that failed.

## Phase 4: runs, chunks and resume (`homeharvest/tracking/run.py`, public `track()`)

```py
def track(name, location, *, db="homeharvest.db", split_by=None, resume=True, reset=False,
          **scrape_kwargs) -> TrackResult
```

1. Load/create the watch; resume the latest unfinished run of this watch if `resume` and it's under 6 days old,
   otherwise start a new run.
2. Build the chunk list: one chunk per listing type in `listing_type`. `listing_type=None` means for_sale,
   for_rent, pending and sold; off_market is left out, since it covers every unlisted home and would make each
   run huge. With `split_by="zip_code"`, also one chunk per ZIP from
   `homeharvest.locations.get_zip_codes(...)` (falls back to no ZIP split, with a warning, if that returns nothing).
3. For each chunk not yet `done` in this run: call `_scrape_property(..., _with_stats=True)` with
   `limit=None` and `return_type="pydantic"`, flatten with `homeharvest.utils.process_result`, upsert, then
   `mark_removed` only if the chunk is complete, then `record_chunk`. Commit after every chunk.
4. Exceptions: `BlockedError` → status `blocked`, `NetworkError` → `network_down`, `KeyboardInterrupt`/`SIGTERM` →
   `interrupted`; stop, keep what's saved, return a result whose `status` says so. The next `track()` call resumes.
   Other errors in one chunk → that chunk `failed`, continue with the next, run status `partial`.
5. Return `TrackResult(run_id, status, summary: dict[change_type, count], changes: DataFrame,
   listings: DataFrame)`.

Opt-in data (`include_property_history`, `include_environmental_risk`, ...): pass through, but only for listings
that are `new` or changed, by running a second pass on those property ids (reuse
`RealtorScraper.get_property_details`). This mirrors LandWatch's "fetch details only for new/changed listings"
and keeps weekly runs cheap.

**Done when**: a test interrupts a run after chunk 1 (fake API raises `BlockedError` on chunk 2), the second
`track()` call resumes, skips chunk 1 and finishes; the change counts equal an uninterrupted run's.

## Phase 5: reading and exporting

- `get_changes(db, watch, since=None, run_id=None, types=None) -> DataFrame` (joins the latest row for address,
  URL, price).
- `get_history(db, property_id, watch=None) -> DataFrame` (observations timeline).
- `get_listings(db, watch, active=True) -> DataFrame` (same columns as `scrape_property`).
- `export_changes(db, watch, path, run_id=None)`: `.csv`, or `.xlsx` with sheets Listings / Changes / History /
  Run summary (use pandas `ExcelWriter`; `openpyxl` stays optional and the error says how to install it).
- Map of changes: `to_osm_map(get_changes(...), path, keywords=None)` should work. Add a `change_type` column so
  the popup shows it, and color price cuts in red by passing them as the highlighted set.

**Done when**: tests export CSV and (if openpyxl is installed) Excel from a two-run store and check the rows.

## Phase 6: command line and scheduling

- `python -m homeharvest.tracking NAME --location ... [--listing-type ...] [--property-type ...] [--split-by zip_code]
  [--db PATH] [--export changes.xlsx] [--new-run] [--reset]`, printing the summary. Exit code 0 for done/partial,
  1 otherwise (so schedulers can alert).
- README section "Tracking listings over time" with the Python and CLI examples, the removal rule, and how to
  schedule it:
  - macOS/Linux cron: `0 2 * * 0 cd /path/to/project && /path/to/python -m homeharvest.tracking lehigh-land ...`
  - macOS note from the LandWatch README: scheduled jobs can't read `~/Downloads`, `~/Documents` or `~/Desktop`;
    keep the database elsewhere, and use `caffeinate -i` for long runs.

**Done when**: a test runs the CLI through `subprocess` with the fake API patched in (or calls `main(argv)`
directly) and checks the printed summary and exit code.

## Phase 7: live check (on your machine)

1. `pip install -e .` from the merged repo.
2. Run a small watch twice a few hours apart:
   `python -m homeharvest.tracking test-small --location "33971" --listing-type for_sale pending sold --export changes.csv`
3. Check: the first run reports everything as `new`; the second reports few changes and no mass `removed`.
4. Interrupt a run with Ctrl-C and run it again: it should say it's resuming.

---

## Tests to add (all offline)

`tests/test_offline_tracking.py` with the `fake_api` fixture pattern from the other offline tests:

1. first run: every listing `new`, listings table filled, observations written
2. price cut and increase (amount and pct), changes under $1 ignored
3. for_sale → pending (`under_contract`), pending → for_sale (`back_on_market`), → sold (`sold`)
4. listing missing in a complete chunk → `removed`; missing in a capped, limit-truncated or failed chunk → nothing
5. removed then seen again → `relisted`; `listing_id` changed → `relisted`
6. watch params changed → `ValueError`; `reset=True` starts over
7. interrupted run resumes and skips finished chunks; results equal an uninterrupted run
8. `split_by="zip_code"` builds one chunk per ZIP (fake aggregation) and falls back when there are none
9. `get_changes` / `get_history` / `get_listings` / `export_changes`
10. two threads writing to the store don't corrupt it (WAL + lock)

## Out of scope (for now)

- Tracking LandWatch listings in the same database (possible later: same schema with a `source` column).
- Notifications (email/Slack) when something changes; the exit code and the changes file are the hooks for it.
- Cloud scheduling: Realtor.com may block cloud IPs, so schedule on your own machine first.

## Effort

Roughly one focused session per two phases: phases 1-3 (core), 4-5 (runs and exports), 6-7 (CLI, docs, live
check). Each phase is independently testable and committable.

---

## Prompt to hand this to Claude

> Implement `docs/PLAN-listing-tracking.md` in this repository, one phase at a time, one commit per phase.
> Before each phase, re-read the files it names. Keep all existing tests passing and add the offline tests the
> plan lists (Realtor.com is not reachable from here, so use `tests/fakes.py`). After each phase, show me the test
> results and stop so I can review before the next one. Don't change the public behavior of `scrape_property`.
