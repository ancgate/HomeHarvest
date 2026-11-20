"""Read tracked data back as DataFrames, and export changes to CSV, Excel or maps."""

from __future__ import annotations

import json
import os
from typing import Any, Iterable

import pandas as pd

from .store import ListingStore

CHANGE_COLUMNS = ["watch", "run_id", "at", "change_type", "old_value", "new_value", "amount", "pct", "property_id"]

_LABELS = {
    "new": "New listing", "price_cut": "Price cut", "price_increase": "Price increase",
    "under_contract": "Under contract", "back_on_market": "Back on market", "sold": "Sold",
    "status_change": "Status change", "relisted": "Relisted", "removed": "Removed",
}


def change_label(row: dict) -> str | None:
    """'Price cut: $100,000 -> $95,000 (-5.0%)' style text for one change row."""
    kind = row.get("change_type")
    if not kind:
        return None
    label = _LABELS.get(kind, kind.replace("_", " ").capitalize())

    def money(value):
        try:
            return f"${float(value):,.0f}"
        except (TypeError, ValueError):
            return value

    if kind in ("price_cut", "price_increase"):
        pct = row.get("pct")
        return f"{label}: {money(row.get('old_value'))} -> {money(row.get('new_value'))}" + (
            f" ({pct:+.1f}%)" if isinstance(pct, (int, float)) else "")
    if kind in ("under_contract", "back_on_market", "status_change"):
        return f"{label}: {row.get('old_value')} -> {row.get('new_value')}"
    if kind == "sold" and row.get("new_value"):
        return f"{label} for {money(row.get('new_value'))}"
    return label


def _open(db) -> tuple[ListingStore, bool]:
    if isinstance(db, ListingStore):
        return db, False
    if not os.path.exists(db):
        raise FileNotFoundError(f"No tracking database at {db!r}. Run track(...) first.")
    return ListingStore(db), True


def changes_frame(store: ListingStore, watch: str | None = None, since: str | None = None,
                  run_id: int | None = None, types: Iterable[str] | None = None) -> pd.DataFrame:
    records = []
    for change in store.changes(watch=watch, since=since, run_id=run_id, types=types):
        record = {column: change[column] for column in CHANGE_COLUMNS}
        listing = json.loads(change["row_json"]) if change["row_json"] else {}
        record.update({k: v for k, v in listing.items() if k not in record})
        record["change"] = change_label(record)
        records.append(record)
    if not records:
        return pd.DataFrame(columns=CHANGE_COLUMNS + ["change"])
    df = pd.DataFrame(records)
    first = CHANGE_COLUMNS + ["change"]
    return df[first + [c for c in df.columns if c not in first]]


def listings_frame(store: ListingStore, watch: str, active: bool | None = True) -> pd.DataFrame:
    records = []
    for listing in store.listings(watch, active=active):
        record = json.loads(listing["row_json"])
        record.update({"first_seen": listing["first_seen"], "last_seen": listing["last_seen"],
                       "active": bool(listing["active"]), "removed_at": listing["removed_at"]})
        records.append(record)
    return pd.DataFrame(records)


def get_changes(db, watch: str | None = None, since: str | None = None, run_id: int | None = None,
                types: Iterable[str] | str | None = None) -> pd.DataFrame:
    """Changes recorded by track(), newest last, with the listing's latest data alongside.

    :param since: ISO date/time, e.g. "2026-11-01".
    :param types: change types to keep, e.g. ["price_cut", "new"] (see homeharvest.tracking.CHANGE_TYPES).
    """
    store, owned = _open(db)
    try:
        return changes_frame(store, watch, since, run_id, [types] if isinstance(types, str) else types)
    finally:
        if owned:
            store.close()


def get_listings(db, watch: str, active: bool | None = True) -> pd.DataFrame:
    """Listings of a watch (active ones by default; False = removed, None = all), latest data per listing."""
    store, owned = _open(db)
    try:
        return listings_frame(store, watch, active)
    finally:
        if owned:
            store.close()


def get_history(db, property_id: Any, watch: str | None = None) -> pd.DataFrame:
    """Price/status timeline of one property as observed by track()."""
    store, owned = _open(db)
    try:
        rows = store.history(str(property_id), watch)
        return pd.DataFrame([dict(r) for r in rows],
                            columns=["watch", "property_id", "observed_at", "status", "list_price", "sold_price"])
    finally:
        if owned:
            store.close()


def get_runs(db, watch: str) -> pd.DataFrame:
    """Every run of a watch with its status and stats."""
    store, owned = _open(db)
    try:
        rows = [dict(r) for r in store.runs(watch)]
        for r in rows:
            r.update(json.loads(r.pop("stats_json") or "{}"))
        return pd.DataFrame(rows)
    finally:
        if owned:
            store.close()


def _flat(df: pd.DataFrame) -> pd.DataFrame:
    """Lists/dicts -> JSON text, so CSV and Excel cells stay readable."""
    out = df.copy()
    for column in out.columns:
        if out[column].map(lambda v: isinstance(v, (list, dict))).any():
            out[column] = out[column].map(lambda v: json.dumps(v, default=str) if isinstance(v, (list, dict)) else v)
    return out


def export_changes(db, watch: str, path: str | os.PathLike, run_id: int | None = None,
                   since: str | None = None) -> str:
    """Write changes to .csv, or a workbook to .xlsx (sheets: Changes, Listings, History, Runs)."""
    store, owned = _open(db)
    try:
        if run_id is None and since is None:
            runs = store.runs(watch)
            run_id = runs[-1]["id"] if runs else None
        changes = changes_frame(store, watch, since=since, run_id=run_id)
        target = os.fspath(path)
        directory = os.path.dirname(target)
        if directory:
            os.makedirs(directory, exist_ok=True)
        if target.lower().endswith(".xlsx"):
            try:
                writer = pd.ExcelWriter(target)
            except (ImportError, ValueError) as exc:
                raise ImportError("Excel export needs openpyxl or xlsxwriter: pip install openpyxl") from exc
            history = pd.DataFrame([dict(r) for r in store.conn.execute(
                "SELECT * FROM observations WHERE watch=? ORDER BY property_id, observed_at", (watch,))])
            with writer:
                _flat(changes).to_excel(writer, sheet_name="Changes", index=False)
                _flat(listings_frame(store, watch)).to_excel(writer, sheet_name="Listings", index=False)
                history.to_excel(writer, sheet_name="History", index=False)
                get_runs(store, watch).to_excel(writer, sheet_name="Runs", index=False)
        else:
            _flat(changes).to_csv(target, index=False)
        return target
    finally:
        if owned:
            store.close()


def map_changes(db, watch: str, path: str | os.PathLike, run_id: int | None = None, since: str | None = None,
                highlight: Iterable[str] = ("price_cut", "new", "back_on_market"), **map_kwargs) -> str:
    """Map the listings that changed (one point per listing, latest change), highlighting some change types.

    Uses homeharvest.export_map, so the format follows the file extension (.html, .kml, .geojson, .csv).
    """
    from ..maps import export_map

    store, owned = _open(db)
    try:
        if run_id is None and since is None:
            runs = store.runs(watch)
            run_id = runs[-1]["id"] if runs else None
        changes = changes_frame(store, watch, since=since, run_id=run_id)
    finally:
        if owned:
            store.close()
    if not changes.empty:
        changes = changes.drop_duplicates("property_id", keep="last").reset_index(drop=True)
        wanted = set(highlight)
        changes["matched_keywords"] = [
            label if kind in wanted else None for kind, label in zip(changes["change_type"], changes["change"])]
    map_kwargs.setdefault("title" if not os.fspath(path).lower().endswith(".kml") else "name",
                          f"{watch}: {len(changes)} changed listings")
    return export_map(changes, path, **map_kwargs)
