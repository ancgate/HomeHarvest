"""
SQLite storage for tracked searches ("watches"): every listing seen, its price/status timeline,
the changes detected on each run, and run/chunk progress for resuming.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
    name        TEXT PRIMARY KEY,
    params_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    watch       TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    stats_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_watch ON runs(watch, id);
CREATE TABLE IF NOT EXISTS run_chunks (
    run_id         INTEGER,
    chunk          TEXT,
    status         TEXT,
    reported_total INTEGER,
    fetched        INTEGER,
    complete       INTEGER,
    error          TEXT,
    finished_at    TEXT,
    PRIMARY KEY (run_id, chunk)
);
CREATE TABLE IF NOT EXISTS listings (
    watch       TEXT,
    property_id TEXT,
    listing_id  TEXT,
    chunk       TEXT,
    status      TEXT,
    list_price  REAL,
    sold_price  REAL,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    removed_at  TEXT,
    row_json    TEXT NOT NULL,
    PRIMARY KEY (watch, property_id)
);
CREATE INDEX IF NOT EXISTS idx_listings_active ON listings(watch, active);
CREATE TABLE IF NOT EXISTS observations (
    watch       TEXT,
    property_id TEXT,
    observed_at TEXT,
    status      TEXT,
    list_price  REAL,
    sold_price  REAL
);
CREATE INDEX IF NOT EXISTS idx_obs_property ON observations(watch, property_id);
CREATE TABLE IF NOT EXISTS changes (
    run_id      INTEGER,
    watch       TEXT,
    property_id TEXT,
    change_type TEXT,
    old_value   TEXT,
    new_value   TEXT,
    amount      REAL,
    pct         REAL,
    at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_changes_watch ON changes(watch, at);
CREATE INDEX IF NOT EXISTS idx_changes_run ON changes(run_id);
"""

#: Every change type the store records
CHANGE_TYPES = ("new", "price_cut", "price_increase", "under_contract", "back_on_market", "sold",
                "status_change", "relisted", "removed")

ACTIVE_STATUSES = {"FOR_SALE", "FOR_RENT"}
CONTRACT_STATUSES = {"PENDING", "CONTINGENT"}
#: Price changes smaller than this (dollars) are ignored
MIN_PRICE_CHANGE = 1.0
#: Runs that started longer ago than this are not resumed
RESUME_MAX_AGE_DAYS = 6


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def _fmt(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


def canonical_params(params: dict) -> str:
    """Stable JSON for a watch's search parameters (so reruns can be compared)."""
    return json.dumps(params, sort_keys=True, default=str, separators=(",", ":"))


class ListingStore:
    """All tracking data in one SQLite file. Safe to share between threads (one writer at a time)."""

    def __init__(self, path: str | Path, clock: Callable[[], datetime] | None = None):
        path = Path(path)
        if path.parent and str(path.parent) not in ("", "."):
            path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._clock = clock
        self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=60)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.lock = threading.RLock()

    def close(self) -> None:
        self.conn.close()

    def now(self) -> str:
        moment = (self._clock or (lambda: datetime.now(timezone.utc)))()
        return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    @staticmethod
    def _parse(ts: str) -> datetime:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)

    # -- watches -----------------------------------------------------------
    def get_watch(self, name: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM watches WHERE name=?", (name,)).fetchone()

    def list_watches(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM watches ORDER BY name").fetchall()

    def get_or_create_watch(self, name: str, params: dict, reset: bool = False) -> sqlite3.Row:
        """Create the watch, or check that its saved parameters match. ``reset`` wipes it first."""
        params_json = canonical_params(params)
        if reset:
            self.delete_watch(name)
        existing = self.get_watch(name)
        if existing is not None:
            if existing["params_json"] != params_json:
                saved = json.loads(existing["params_json"])
                diff = sorted(k for k in set(saved) | set(params) if saved.get(k) != json.loads(params_json).get(k))
                raise ValueError(
                    f"Watch {name!r} was created with different search parameters ({', '.join(diff)}). "
                    "Changing the search would make listings look new or removed. Use another name, "
                    "or reset=True to start this watch over."
                )
            return existing
        with self.lock, self.conn:
            self.conn.execute("INSERT INTO watches VALUES (?, ?, ?)", (name, params_json, self.now()))
        return self.get_watch(name)

    def delete_watch(self, name: str) -> None:
        with self.lock, self.conn:
            runs = [r["id"] for r in self.conn.execute("SELECT id FROM runs WHERE watch=?", (name,))]
            self.conn.executemany("DELETE FROM run_chunks WHERE run_id=?", [(r,) for r in runs])
            for table in ("runs", "listings", "observations", "changes", "watches"):
                column = "name" if table == "watches" else "watch"
                self.conn.execute(f"DELETE FROM {table} WHERE {column}=?", (name,))

    # -- runs ----------------------------------------------------------------
    def start_run(self, watch: str) -> sqlite3.Row:
        with self.lock, self.conn:
            cur = self.conn.execute("INSERT INTO runs(watch, started_at, status) VALUES (?, ?, 'running')",
                                    (watch, self.now()))
        return self.get_run(cur.lastrowid)

    def get_run(self, run_id: int) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()

    def runs(self, watch: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM runs WHERE watch=? ORDER BY id", (watch,)).fetchall()

    def unfinished_run(self, watch: str, max_age_days: int = RESUME_MAX_AGE_DAYS) -> sqlite3.Row | None:
        """The watch's latest run if it never finished (interrupted, blocked, offline) and is recent."""
        row = self.conn.execute("SELECT * FROM runs WHERE watch=? ORDER BY id DESC LIMIT 1", (watch,)).fetchone()
        if not row or row["finished_at"] is not None:
            return None
        if self._parse(self.now()) - self._parse(row["started_at"]) > timedelta(days=max_age_days):
            return None
        return row

    def set_run_status(self, run_id: int, status: str, stats: dict | None = None) -> None:
        with self.lock, self.conn:
            self.conn.execute("UPDATE runs SET status=?, stats_json=? WHERE id=?",
                              (status, json.dumps(stats or {}, default=str), run_id))

    def finish_run(self, run_id: int, status: str, stats: dict | None = None) -> None:
        with self.lock, self.conn:
            self.conn.execute("UPDATE runs SET finished_at=?, status=?, stats_json=? WHERE id=?",
                              (self.now(), status, json.dumps(stats or {}, default=str), run_id))

    # -- chunks ----------------------------------------------------------------
    def record_chunk(self, run_id: int, chunk: str, status: str, reported_total: int | None = None,
                     fetched: int | None = None, complete: bool = False, error: str | None = None) -> None:
        with self.lock, self.conn:
            self.conn.execute("INSERT OR REPLACE INTO run_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                              (run_id, chunk, status, reported_total, fetched, int(bool(complete)), error,
                               self.now()))

    def chunks(self, run_id: int) -> dict[str, sqlite3.Row]:
        return {r["chunk"]: r for r in self.conn.execute("SELECT * FROM run_chunks WHERE run_id=?", (run_id,))}

    def done_chunks(self, run_id: int) -> set[str]:
        return {c for c, r in self.chunks(run_id).items() if r["status"] == "done"}

    def complete_chunks(self, run_id: int) -> set[str]:
        return {c for c, r in self.chunks(run_id).items() if r["status"] == "done" and r["complete"]}

    # -- listings & change detection --------------------------------------------
    def _change(self, run_id, watch, property_id, kind, old=None, new=None, amount=None, pct=None, at=None):
        self.conn.execute("INSERT INTO changes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (run_id, watch, property_id, kind, _fmt(old), _fmt(new), amount, pct, at or self.now()))

    def upsert_listings(self, run_id: int, watch: str, chunk: str, rows: Iterable[dict]) -> list[dict]:
        """Save one chunk's listings and record what changed. Returns the changes (as dicts)."""
        recorded: list[dict] = []
        with self.lock, self.conn:
            for row in rows:
                property_id = row.get("property_id")
                if property_id is None:
                    continue
                property_id = str(property_id)
                ts = self.now()
                status = str(row.get("status") or "").upper() or None
                list_price, sold_price = _num(row.get("list_price")), _num(row.get("sold_price"))
                listing_id = _fmt(row.get("listing_id"))
                old = self.conn.execute("SELECT * FROM listings WHERE watch=? AND property_id=?",
                                        (watch, property_id)).fetchone()

                found: list[tuple] = []
                if old is None:
                    found.append(("new", None, list_price if status != "SOLD" else sold_price, None, None))
                else:
                    old_status = old["status"]
                    if not old["active"]:
                        if status == "SOLD" and old_status != "SOLD":
                            found.append(("sold", old["list_price"], sold_price, None, None))
                        else:
                            found.append(("relisted", None, list_price, None, None))
                    else:
                        if status != old_status:
                            if old_status in ACTIVE_STATUSES and status in CONTRACT_STATUSES:
                                found.append(("under_contract", old_status, status, None, None))
                            elif old_status in CONTRACT_STATUSES and status in ACTIVE_STATUSES:
                                found.append(("back_on_market", old_status, status, None, None))
                            elif status == "SOLD":
                                found.append(("sold", old["list_price"], sold_price, None, None))
                            else:
                                found.append(("status_change", old_status, status, None, None))
                        elif (old["listing_id"] and listing_id and old["listing_id"] != listing_id
                              and status != "SOLD"):
                            found.append(("relisted", old["listing_id"], listing_id, None, None))
                    old_price = old["list_price"]
                    if (status != "SOLD" and old_price and list_price is not None
                            and abs(list_price - old_price) >= MIN_PRICE_CHANGE):
                        amount = list_price - old_price
                        kind = "price_cut" if amount < 0 else "price_increase"
                        found.append((kind, old_price, list_price, amount, round(amount / old_price * 100, 2)))

                for kind, old_value, new_value, amount, pct in found:
                    self._change(run_id, watch, property_id, kind, old_value, new_value, amount, pct, ts)
                    recorded.append({"property_id": property_id, "change_type": kind, "old_value": _fmt(old_value),
                                     "new_value": _fmt(new_value), "amount": amount, "pct": pct})
                if found:
                    self.conn.execute("INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?)",
                                      (watch, property_id, ts, status, list_price, sold_price))

                self.conn.execute(
                    """INSERT INTO listings (watch, property_id, listing_id, chunk, status, list_price, sold_price,
                                             first_seen, last_seen, active, removed_at, row_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, ?)
                       ON CONFLICT(watch, property_id) DO UPDATE SET
                           listing_id=excluded.listing_id, chunk=excluded.chunk, status=excluded.status,
                           list_price=excluded.list_price, sold_price=excluded.sold_price,
                           last_seen=excluded.last_seen, active=1, removed_at=NULL, row_json=excluded.row_json""",
                    (watch, property_id, listing_id, chunk, status, list_price, sold_price, ts, ts,
                     json.dumps(row, default=str)),
                )
        return recorded

    def update_rows(self, watch: str, rows: Iterable[dict]) -> None:
        """Replace the stored row data (e.g. after fetching opt-in details), without change detection."""
        with self.lock, self.conn:
            for row in rows:
                if row.get("property_id") is not None:
                    self.conn.execute("UPDATE listings SET row_json=? WHERE watch=? AND property_id=?",
                                      (json.dumps(row, default=str), watch, str(row["property_id"])))

    def mark_removed(self, run_id: int, watch: str, complete_chunks: set[str], run_started: str) -> int:
        """Deactivate listings not seen this run whose chunk was searched completely.

        Sold listings that drop out (e.g. past the past_days window) are deactivated silently: they
        already left the market. Returns the number of ``removed`` changes recorded.
        """
        if not complete_chunks:
            return 0
        ts = self.now()
        marks = ",".join("?" * len(complete_chunks))
        with self.lock, self.conn:
            rows = self.conn.execute(
                f"""SELECT property_id, status, list_price FROM listings
                    WHERE watch=? AND active=1 AND last_seen<? AND chunk IN ({marks})""",
                (watch, run_started, *complete_chunks)).fetchall()
            removed = 0
            for row in rows:
                if row["status"] != "SOLD":
                    self._change(run_id, watch, row["property_id"], "removed", row["list_price"], None, at=ts)
                    removed += 1
                self.conn.execute("UPDATE listings SET active=0, removed_at=? WHERE watch=? AND property_id=?",
                                  (ts, watch, row["property_id"]))
        return removed

    # -- reading -----------------------------------------------------------------
    def listings(self, watch: str, active: bool | None = True) -> list[sqlite3.Row]:
        sql, params = "SELECT * FROM listings WHERE watch=?", [watch]
        if active is not None:
            sql += " AND active=?"
            params.append(int(active))
        return self.conn.execute(sql + " ORDER BY property_id", params).fetchall()

    def changes(self, watch: str | None = None, since: str | None = None, run_id: int | None = None,
                types: Iterable[str] | None = None) -> list[sqlite3.Row]:
        sql = """SELECT c.*, l.row_json FROM changes c
                 LEFT JOIN listings l ON l.watch = c.watch AND l.property_id = c.property_id WHERE 1=1"""
        params: list = []
        if watch is not None:
            sql += " AND c.watch=?"
            params.append(watch)
        if since is not None:
            sql += " AND c.at>=?"
            params.append(since)
        if run_id is not None:
            sql += " AND c.run_id=?"
            params.append(run_id)
        types = list(types or [])
        if types:
            sql += f" AND c.change_type IN ({','.join('?' * len(types))})"
            params += types
        return self.conn.execute(sql + " ORDER BY c.at, c.property_id, c.rowid", params).fetchall()

    def history(self, property_id: str, watch: str | None = None) -> list[sqlite3.Row]:
        sql, params = "SELECT * FROM observations WHERE property_id=?", [str(property_id)]
        if watch is not None:
            sql += " AND watch=?"
            params.append(watch)
        return self.conn.execute(sql + " ORDER BY observed_at", params).fetchall()
