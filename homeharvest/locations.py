"""
homeharvest.locations
~~~~~~~~~~~~~~~~~~~~~

Look up states, counties, cities and ZIP codes, and aggregate listings by location.

* ``get_states()`` - static list of US states (+ DC, optional territories) with FIPS codes.
* ``get_counties()``, ``get_cities()``, ``get_zip_codes()`` - live from Realtor.com: the
  locations that have listings, optionally with listing counts.
* ``get_location_hierarchy()`` - state -> county -> city -> ZIP rows (live).
* ``aggregate_locations()`` - group properties you already scraped by state / county / city /
  ZIP with counts and median prices (offline).

Note on "towns": Realtor.com files towns, villages, boroughs and census-designated places
(e.g. Lehigh Acres, FL) under ``city``, so they are returned by ``get_cities()``.
"""

from __future__ import annotations

import warnings
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable

import pandas as pd

__all__ = [
    "get_states",
    "get_state",
    "get_counties",
    "get_cities",
    "get_zip_codes",
    "get_location_hierarchy",
    "aggregate_locations",
]

# (code, name, FIPS)
_STATES = [
    ("AL", "Alabama", "01"), ("AK", "Alaska", "02"), ("AZ", "Arizona", "04"), ("AR", "Arkansas", "05"),
    ("CA", "California", "06"), ("CO", "Colorado", "08"), ("CT", "Connecticut", "09"), ("DE", "Delaware", "10"),
    ("DC", "District of Columbia", "11"), ("FL", "Florida", "12"), ("GA", "Georgia", "13"), ("HI", "Hawaii", "15"),
    ("ID", "Idaho", "16"), ("IL", "Illinois", "17"), ("IN", "Indiana", "18"), ("IA", "Iowa", "19"),
    ("KS", "Kansas", "20"), ("KY", "Kentucky", "21"), ("LA", "Louisiana", "22"), ("ME", "Maine", "23"),
    ("MD", "Maryland", "24"), ("MA", "Massachusetts", "25"), ("MI", "Michigan", "26"), ("MN", "Minnesota", "27"),
    ("MS", "Mississippi", "28"), ("MO", "Missouri", "29"), ("MT", "Montana", "30"), ("NE", "Nebraska", "31"),
    ("NV", "Nevada", "32"), ("NH", "New Hampshire", "33"), ("NJ", "New Jersey", "34"), ("NM", "New Mexico", "35"),
    ("NY", "New York", "36"), ("NC", "North Carolina", "37"), ("ND", "North Dakota", "38"), ("OH", "Ohio", "39"),
    ("OK", "Oklahoma", "40"), ("OR", "Oregon", "41"), ("PA", "Pennsylvania", "42"), ("RI", "Rhode Island", "44"),
    ("SC", "South Carolina", "45"), ("SD", "South Dakota", "46"), ("TN", "Tennessee", "47"), ("TX", "Texas", "48"),
    ("UT", "Utah", "49"), ("VT", "Vermont", "50"), ("VA", "Virginia", "51"), ("WA", "Washington", "53"),
    ("WV", "West Virginia", "54"), ("WI", "Wisconsin", "55"), ("WY", "Wyoming", "56"),
]
_TERRITORIES = [
    ("AS", "American Samoa", "60"), ("GU", "Guam", "66"), ("MP", "Northern Mariana Islands", "69"),
    ("PR", "Puerto Rico", "72"), ("VI", "U.S. Virgin Islands", "78"),
]

_COUNTY_WORDS = ("county", "parish", "borough", "census area", "municipality", "city and borough")


# ---------------------------------------------------------------------------
# States (static)
# ---------------------------------------------------------------------------

def get_states(include_territories: bool = False, return_type: str = "list") -> list[dict] | pd.DataFrame:
    """US states and DC (plus territories if requested) as dicts with code, name and fips.

    :param return_type: "list" (default) or "pandas".
    """
    rows = [{"code": c, "name": n, "fips": f} for c, n, f in _STATES]
    if include_territories:
        rows += [{"code": c, "name": n, "fips": f} for c, n, f in _TERRITORIES]
    return pd.DataFrame(rows) if return_type == "pandas" else rows


def get_state(state: str) -> dict:
    """Look up a state by code ("FL"), name ("Florida") or FIPS ("12")."""
    key = str(state).strip().lower()
    for code, name, fips in _STATES + _TERRITORIES:
        if key in (code.lower(), name.lower(), fips):
            return {"code": code, "name": name, "fips": fips}
    raise ValueError(f"Unknown state {state!r}. Use a 2-letter code, full name or FIPS code.")


# ---------------------------------------------------------------------------
# Realtor.com aggregates (live)
# ---------------------------------------------------------------------------

def _county_location(county: str, state_code: str) -> str:
    name = county.strip()
    if not any(word in name.lower() for word in _COUNTY_WORDS):
        name += " Parish" if state_code == "LA" else " County"
    return f"{name}, {state_code}"


def _scope_location(state: str | None, county: str | None, city: str | None, zip_code: str | None = None) -> str:
    state_info = get_state(state) if state else None
    if zip_code:
        return str(zip_code)
    if city:
        if not state_info:
            raise ValueError("Pass state= together with city= (e.g. city='Lehigh Acres', state='FL').")
        return f"{city}, {state_info['code']}"
    if county:
        if not state_info:
            raise ValueError("Pass state= together with county= (e.g. county='Lee', state='FL').")
        return _county_location(county, state_info["code"])
    if state_info:
        return state_info["name"]
    raise ValueError("Pass at least state= (and optionally county= / city=).")


def _parse_buckets(aggregation: Any, field: str) -> list[tuple[str, int | None]]:
    """Read one field's buckets from Realtor.com's aggregation JSON (tolerant of shape)."""
    if not isinstance(aggregation, dict):
        return []
    node = aggregation.get(field)
    if node is None:
        node = aggregation.get(f"{field}s") or aggregation.get(field.replace("_", ""))
    if isinstance(node, dict) and isinstance(node.get("buckets"), list):
        node = node["buckets"]

    buckets: list[tuple[str, int | None]] = []
    if isinstance(node, dict):  # {"Lee": 120, ...}
        for key, count in node.items():
            buckets.append((str(key), count if isinstance(count, int) else _first_int(count)))
    elif isinstance(node, list):  # [{"key": "Lee", "count": 120}, ...]
        for item in node:
            if isinstance(item, dict):
                key = next((item[k] for k in ("key", "name", "value", field) if item.get(k) not in (None, "")), None)
                if key is not None:
                    buckets.append((str(key), _first_int(item)))
            elif isinstance(item, str):
                buckets.append((item, None))
    return [(k.strip(), c) for k, c in buckets if k and k.strip()]


def _first_int(item: Any) -> int | None:
    if isinstance(item, dict):
        for key in ("count", "doc_count", "total", "listing_count"):
            if isinstance(item.get(key), (int, float)):
                return int(item[key])
    return None


def _make_scraper(location: str, listing_type, proxy: str | None):
    from .core.scrapers import ScraperInput
    from .core.scrapers.models import ListingType
    from .core.scrapers.realtor import RealtorScraper
    from .utils import validate_input

    validate_input(listing_type)
    if listing_type is None:
        converted = None
    elif isinstance(listing_type, list):
        converted = [ListingType(lt.upper()) for lt in listing_type]
    else:
        converted = ListingType(listing_type.upper())
    return RealtorScraper(ScraperInput(location=location, listing_type=converted, proxy=proxy))


def _aggregate(location: str, field: str, listing_type=None, proxy: str | None = None,
               size: int = 2000) -> list[tuple[str, int | None]] | None:
    """Buckets for one field at a location, or None if Realtor.com returned no aggregation."""
    scraper = _make_scraper(location, listing_type, proxy)
    aggregation = scraper.get_location_aggregates([field], size=size)
    if aggregation is None:
        return None
    return _parse_buckets(aggregation, field)


def _fallback_from_listings(location: str, column: str, listing_type, proxy, limit: int) -> list[tuple[str, int]]:
    from . import scrape_property

    warnings.warn(
        f"Realtor.com returned no {column} aggregation for {location!r}; falling back to the locations of up "
        f"to {limit} listings (results may be incomplete).",
        RuntimeWarning,
        stacklevel=3,
    )
    df = scrape_property(location=location, listing_type=listing_type or "for_sale", proxy=proxy, limit=limit)
    if df.empty or column not in df.columns:
        return []
    counts = df[column].dropna().astype(str).value_counts()
    return [(k, int(v)) for k, v in counts.items()]


def _result(buckets: list[tuple[str, int | None]], label: str, with_counts: bool):
    merged: dict[str, int | None] = {}
    for key, count in buckets:
        if key in merged and isinstance(count, int):
            merged[key] = (merged[key] or 0) + count
        else:
            merged.setdefault(key, count)
    names = sorted(merged, key=lambda s: s.lower())
    if with_counts:
        return pd.DataFrame({label: names, "listing_count": [merged[n] for n in names]})
    return names


def _lookup(field: str, column: str, label: str, state, county, city, listing_type, with_counts, proxy,
            fallback_limit: int):
    location = _scope_location(state, county, city)
    buckets = _aggregate(location, field, listing_type, proxy)
    if not buckets:
        buckets = _fallback_from_listings(location, column, listing_type, proxy, fallback_limit)
    return _result(buckets, label, with_counts)


def get_counties(state: str, *, listing_type: str | list[str] | None = None, with_counts: bool = False,
                 proxy: str | None = None, fallback_limit: int = 10000) -> list[str] | pd.DataFrame:
    """Counties in a state that have listings on Realtor.com.

    :param state: State code, name or FIPS ("FL", "Florida", "12").
    :param listing_type: Count only these listing types (default: for_sale, for_rent, sold, off_market).
    :param with_counts: Return a DataFrame with a listing_count column instead of a list of names.
    """
    return _lookup("county", "county", "county", state, None, None, listing_type, with_counts, proxy, fallback_limit)


def get_cities(state: str, county: str | None = None, *, listing_type: str | list[str] | None = None,
               with_counts: bool = False, proxy: str | None = None,
               fallback_limit: int = 10000) -> list[str] | pd.DataFrame:
    """Cities (including towns, villages and CDPs) in a state or county that have listings."""
    return _lookup("city", "city", "city", state, county, None, listing_type, with_counts, proxy, fallback_limit)


def get_zip_codes(state: str | None = None, county: str | None = None, city: str | None = None, *,
                  listing_type: str | list[str] | None = None, with_counts: bool = False,
                  proxy: str | None = None, fallback_limit: int = 10000) -> list[str] | pd.DataFrame:
    """ZIP codes in a state, county or city that have listings."""
    return _lookup("postal_code", "zip_code", "zip_code", state, county, city, listing_type, with_counts, proxy,
                   fallback_limit)


def get_location_hierarchy(state: str, county: str | None = None, city: str | None = None, *,
                           listing_type: str | list[str] | None = None, include_zip_codes: bool = True,
                           max_requests: int = 300, max_workers: int = 4,
                           proxy: str | None = None) -> pd.DataFrame:
    """State -> county -> city -> ZIP rows for places with listings (live, one request per node).

    Columns: state, state_name, county, city, zip_code, listing_count (count of the deepest level).

    Scope it with ``county=`` (or ``city=``) for large states: a whole state needs one request per
    county plus one per city. ``max_requests`` guards against accidentally huge crawls.
    """
    state_info = get_state(state)
    counties = [county] if county else get_counties(state_info["code"], listing_type=listing_type, proxy=proxy)

    def cities_for(county_name: str) -> list[tuple[str, int | None]]:
        if city:
            return [(city, None)]
        return _aggregate(_scope_location(state_info["code"], county_name, None), "city", listing_type, proxy) or []

    def zips_for(county_name: str, city_name: str) -> list[tuple[str, int | None]]:
        return _aggregate(_scope_location(state_info["code"], None, city_name), "postal_code", listing_type, proxy) or []

    planned = (0 if county else 1) + (0 if city else len(counties))
    if planned > max_requests:
        raise ValueError(_too_many(planned, max_requests))

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        city_lists = list(executor.map(cities_for, counties))

    pairs = [(c, name, count) for c, buckets in zip(counties, city_lists) for name, count in buckets]
    if include_zip_codes:
        planned += len(pairs)
        if planned > max_requests:
            raise ValueError(_too_many(planned, max_requests))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            zip_lists = list(executor.map(lambda p: zips_for(p[0], p[1]), pairs))
    else:
        zip_lists = [[(None, count)] for _, _, count in pairs]

    rows = []
    for (county_name, city_name, _), zips in zip(pairs, zip_lists):
        for zip_code, count in zips or [(None, None)]:
            rows.append({
                "state": state_info["code"], "state_name": state_info["name"], "county": county_name,
                "city": city_name, "zip_code": zip_code, "listing_count": count,
            })
    return pd.DataFrame(rows, columns=["state", "state_name", "county", "city", "zip_code", "listing_count"])


def _too_many(planned: int, max_requests: int) -> str:
    return (
        f"This hierarchy needs about {planned} requests (max_requests={max_requests}). "
        "Narrow it with county=..., set include_zip_codes=False, or raise max_requests."
    )


# ---------------------------------------------------------------------------
# Offline aggregation of scraped properties
# ---------------------------------------------------------------------------

_LEVELS = ("state", "county", "city", "neighborhoods", "zip_code")


def aggregate_locations(properties: Any, by: Iterable[str] = ("state", "county", "city", "zip_code"),
                        as_tree: bool = False):
    """Group scraped properties by location with counts and price statistics.

    :param properties: DataFrame from scrape_property() (or list of Property models / raw dicts).
    :param by: Levels to group by, in order. Any of: state, county, city, neighborhoods, zip_code.
    :param as_tree: Return a nested dict {state: {county: {city: {zip: count}}}} instead of a DataFrame.

    DataFrame columns: the ``by`` levels, listing_count, a count per status (for_sale, sold, ...),
    median_list_price, median_sold_price, median_price_per_sqft, median_days_on_mls,
    min_list_price, max_list_price.
    """
    by = list(by)
    for level in by:
        if level not in _LEVELS:
            raise ValueError(f"Unknown level {level!r}. Use any of: {', '.join(_LEVELS)}")

    if isinstance(properties, pd.DataFrame):
        df = properties.copy()
    else:
        from .maps import _records
        df = pd.DataFrame(_records(properties))

    if df.empty:
        return {} if as_tree else pd.DataFrame(columns=by + ["listing_count"])

    for level in by:
        if level not in df.columns:
            df[level] = pd.NA
        df[level] = df[level].astype("object").where(df[level].notna(), "(unknown)")

    if as_tree:
        tree: dict = {}
        for keys, group in df.groupby(by, sort=True, dropna=False):
            keys = keys if isinstance(keys, tuple) else (keys,)
            node = tree
            for key in keys[:-1]:
                node = node.setdefault(key, {})
            node[keys[-1]] = int(len(group))
        return tree

    numeric = {}
    for column in ("list_price", "sold_price", "price_per_sqft", "days_on_mls"):
        numeric[column] = pd.to_numeric(df[column], errors="coerce") if column in df.columns else pd.Series(
            pd.NA, index=df.index, dtype="Float64")
    work = df[by].copy()
    for column, series in numeric.items():
        work[column] = series
    work["status_key"] = df["status"].astype("object").fillna("UNKNOWN").str.lower() if "status" in df.columns else "unknown"

    grouped = work.groupby(by, sort=True, dropna=False)
    result = grouped.size().rename("listing_count").to_frame()
    status_counts = work.groupby(by + ["status_key"], sort=True, dropna=False).size().unstack(fill_value=0)
    result = result.join(status_counts.add_suffix("_count"))
    result["median_list_price"] = grouped["list_price"].median()
    result["median_sold_price"] = grouped["sold_price"].median()
    result["median_price_per_sqft"] = grouped["price_per_sqft"].median()
    result["median_days_on_mls"] = grouped["days_on_mls"].median()
    result["min_list_price"] = grouped["list_price"].min()
    result["max_list_price"] = grouped["list_price"].max()
    return result.reset_index().sort_values("listing_count", ascending=False, kind="stable").reset_index(drop=True)
