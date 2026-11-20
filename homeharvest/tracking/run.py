"""Run a tracked search ("watch") in resumable chunks and record what changed."""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from .store import ListingStore

#: Listing types tracked when listing_type isn't given. Off-market is left out: it covers every
#: unlisted home and would make each run huge.
DEFAULT_LISTING_TYPES = ["for_sale", "for_rent", "pending", "sold"]
DETAIL_FLAGS = ("include_property_history", "include_environmental_risk", "include_insurance", "include_market_stats")
_DETAIL_GROUPS = {"include_property_history": "property_history", "include_environmental_risk": "environmental_risk",
                  "include_insurance": "insurance", "include_market_stats": "market_stats"}
_NOT_ALLOWED = ("return_type", "limit", "offset", "location")


@dataclass
class Chunk:
    name: str
    location: str
    listing_type: str
    area: tuple[str, str] | None = None  #: keep only results whose (column, value) matches, for ZIP chunks


@dataclass
class TrackResult:
    watch: str
    run_id: int
    status: str            #: done | partial | blocked | network_down | interrupted
    resumed: bool
    summary: dict = field(default_factory=dict)
    changes: pd.DataFrame = field(default_factory=pd.DataFrame)
    listings: pd.DataFrame = field(default_factory=pd.DataFrame)
    chunks: dict = field(default_factory=dict)

    @property
    def finished(self) -> bool:
        return self.status in ("done", "partial")

    def __repr__(self) -> str:
        counts = ", ".join(f"{k}={v}" for k, v in self.summary.items()) or "no changes"
        return (f"TrackResult(watch={self.watch!r}, run={self.run_id}, status={self.status!r}, "
                f"{counts}, listings={len(self.listings)})")


def _norm_place(value: Any) -> str:
    text = str(value or "").strip().lower()
    for suffix in (" county", " parish", " borough"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    return text


def _zip_chunks(location: str, listing_types: list[str], proxy: str | None) -> list[Chunk] | None:
    """One chunk per (listing type, ZIP) for a city/county/state location, or None to not split."""
    from ..core.scrapers import ScraperInput
    from ..core.scrapers.realtor import RealtorScraper
    from ..locations import get_zip_codes

    info = RealtorScraper(ScraperInput(location=location, listing_type=None, proxy=proxy))._get_location_info()
    area_type = (info or {}).get("area_type")
    state = (info or {}).get("state_code")
    try:
        if area_type == "city" and state and info.get("city"):
            zips, area = get_zip_codes(state=state, city=info["city"], proxy=proxy), ("city", info["city"])
        elif area_type == "county" and state and info.get("county"):
            zips, area = get_zip_codes(state=state, county=info["county"], proxy=proxy), ("county", info["county"])
        elif area_type == "state" and state:
            zips, area = get_zip_codes(state=state, proxy=proxy), ("state", state)
        else:
            zips, area = [], None
    except Exception as exc:  # noqa: BLE001 - fall back to one chunk per listing type
        warnings.warn(f"Could not list ZIP codes for {location!r} ({exc}); not splitting by ZIP.",
                      RuntimeWarning, stacklevel=3)
        return None
    if not zips:
        warnings.warn(f"No ZIP codes found for {location!r}; not splitting by ZIP.", RuntimeWarning, stacklevel=3)
        return None
    return [Chunk(f"{lt}|zip={z}", z, lt, area) for lt in listing_types for z in zips]


def _removable_chunks(chunks: list[Chunk], complete: set[str]) -> set[str]:
    """Chunks whose listings may be marked removed.

    A listing can move between listing-type chunks (for sale -> pending -> sold) but never between
    ZIP codes, so removal is only safe in an area (the whole location, or one ZIP) whose chunks all
    finished completely. Otherwise a listing that moved into a failed chunk would look removed.
    """
    areas: dict[str, list[str]] = {}
    for chunk in chunks:
        areas.setdefault(chunk.name.partition("|")[2], []).append(chunk.name)
    return {name for names in areas.values() if all(n in complete for n in names) for name in names}


def _rows(properties, groups: list[str]) -> list[dict]:
    from ..maps import _clean
    from ..utils import process_result

    rows = []
    for prop in properties:
        record = process_result(prop, groups).iloc[0].to_dict()
        rows.append({k: _clean(v) for k, v in record.items()})
    return rows


def _scrape_chunk(chunk: Chunk, scrape_kwargs: dict):
    from .. import scrape_property
    from .._context import SEARCH_STATS

    stats: dict = {}
    token = SEARCH_STATS.set(stats)
    try:
        properties = scrape_property(location=chunk.location, listing_type=chunk.listing_type,
                                     return_type="pydantic", limit=None, **scrape_kwargs)
    finally:
        SEARCH_STATS.reset(token)
    if chunk.area:
        column, value = chunk.area
        wanted = _norm_place(value)
        getter = {"city": lambda p: p.address.city if p.address else None,
                  "state": lambda p: p.address.state if p.address else None,
                  "county": lambda p: p.county}[column]
        properties = [p for p in properties if _norm_place(getter(p)) == wanted]
    return properties, stats


def _enrich(properties, detail_flags: dict, scrape_kwargs: dict, location: str):
    """Fetch opt-in details (history, risk, ...) for these properties only."""
    from ..core.scrapers import ScraperInput
    from ..core.scrapers.realtor import RealtorScraper

    if not properties or not any(detail_flags.values()):
        return properties
    scraper = RealtorScraper(ScraperInput(
        location=location, listing_type=None, proxy=scrape_kwargs.get("proxy"),
        requests_per_second=scrape_kwargs.get("requests_per_second", 2.0),
        max_retries=scrape_kwargs.get("max_retries", 4), **detail_flags))
    return scraper._enrich_with_details(properties)


def track(name: str, location: str, *, db: str = "homeharvest.db", split_by: str | None = None,
          resume: bool = True, reset: bool = False, **scrape_kwargs) -> TrackResult:
    """Run a tracked search and record what changed since its last run.

    :param name: Name of this watch. Its search parameters are saved and must stay the same.
    :param location: Same as scrape_property's location.
    :param db: SQLite file that keeps the history (created if needed).
    :param split_by: None (one chunk per listing type) or "zip_code" (one chunk per type and ZIP, for big
        areas: finer resume points and fewer capped searches).
    :param resume: Resume the watch's last run if it was interrupted (blocked, offline, Ctrl-C).
    :param reset: Forget this watch's history and start over.
    :param scrape_kwargs: Any scrape_property() filters (listing_type, property_type, price_min, features,
        near, past_days, proxy, ...). include_* flags fetch extra data only for new or changed listings.
        return_type, limit and offset are not allowed: tracking always collects every match.
    """
    for key in _NOT_ALLOWED:
        if key in scrape_kwargs:
            raise ValueError(f"track() does not accept {key!r}: it always collects every matching listing.")
    if split_by not in (None, "zip_code"):
        raise ValueError("split_by must be None or 'zip_code'")

    listing_types = scrape_kwargs.pop("listing_type", None) or DEFAULT_LISTING_TYPES
    listing_types = [listing_types] if isinstance(listing_types, str) else list(listing_types)
    detail_flags = {flag: bool(scrape_kwargs.pop(flag, False)) for flag in DETAIL_FLAGS}
    params = {"location": location, "listing_type": listing_types, "split_by": split_by, **scrape_kwargs,
              **{k: v for k, v in detail_flags.items() if v}}

    feature_keys = ("features", "nearby", "keywords")
    groups = (["features"] if any(scrape_kwargs.get(k) for k in feature_keys) else []) + [
        _DETAIL_GROUPS[f] for f, on in detail_flags.items() if on]

    store = ListingStore(db)
    try:
        store.get_or_create_watch(name, params, reset=reset)
        run = store.unfinished_run(name) if resume else None
        resumed = run is not None
        run = run or store.start_run(name)
        run_id, run_started = run["id"], run["started_at"]

        chunks = None
        if split_by == "zip_code":
            chunks = _zip_chunks(location, listing_types, scrape_kwargs.get("proxy"))
        chunks = chunks or [Chunk(lt, location, lt) for lt in listing_types]
        done = store.done_chunks(run_id)

        status = "running"
        for chunk in chunks:
            if chunk.name in done:
                continue
            try:
                properties, stats = _scrape_chunk(chunk, scrape_kwargs)
                if not stats.get("location_found", True):
                    raise ValueError(f"Realtor.com could not find the location {chunk.location!r}")
                rows = _rows(properties, groups)
                changes = store.upsert_listings(run_id, name, chunk.name, rows)
                changed = {c["property_id"] for c in changes}
                if changed and any(detail_flags.values()):
                    targets = [p for p in properties if str(p.property_id) in changed]
                    store.update_rows(name, _rows(_enrich(targets, detail_flags, scrape_kwargs, chunk.location), groups))
                complete = not stats.get("capped") and not stats.get("truncated_by_limit")
                store.record_chunk(run_id, chunk.name, "done", stats.get("reported_total"), len(rows), complete)
            except KeyboardInterrupt:
                status = "interrupted"
                break
            except Exception as exc:  # noqa: BLE001 - classified below
                from ..exceptions import AuthenticationError, NetworkError
                if isinstance(exc, AuthenticationError):   # includes BlockedError
                    status = "blocked"
                    break
                if isinstance(exc, NetworkError):
                    status = "network_down"
                    break
                store.record_chunk(run_id, chunk.name, "failed", error=f"{type(exc).__name__}: {exc}"[:500])
                warnings.warn(f"track({name!r}): chunk {chunk.name!r} failed ({exc}); it will be retried next run.",
                              RuntimeWarning, stacklevel=2)

        chunk_rows = store.chunks(run_id)
        stats = {"chunks": len(chunks), "done": len(store.done_chunks(run_id)),
                 "failed": sum(1 for r in chunk_rows.values() if r["status"] == "failed")}
        if status == "running":  # every chunk was attempted
            stats["removed"] = store.mark_removed(run_id, name, _removable_chunks(chunks, store.complete_chunks(run_id)),
                                                  run_started)
            status = "partial" if stats["failed"] else "done"
            store.finish_run(run_id, status, stats)
        else:  # stopped early: leave the run open so the next call resumes it
            store.set_run_status(run_id, status, stats)

        from .readers import changes_frame, listings_frame
        changes = changes_frame(store, watch=name, run_id=run_id)
        summary = changes["change_type"].value_counts().to_dict() if not changes.empty else {}
        return TrackResult(
            watch=name, run_id=run_id, status=status, resumed=resumed,
            summary={k: int(summary[k]) for k in sorted(summary, key=lambda k: -summary[k])},
            changes=changes, listings=listings_frame(store, name),
            chunks={c: r["status"] for c, r in store.chunks(run_id).items()},
        )
    finally:
        store.close()

