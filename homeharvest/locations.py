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
    "add_place_types",
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
               with_counts: bool = False, place_type: bool = False, proxy: str | None = None,
               overpass_url: str | None = None, fallback_limit: int = 10000) -> list[str] | pd.DataFrame:
    """Cities (including towns, villages and CDPs) in a state or county that have listings.

    :param place_type: Return a DataFrame with OpenStreetMap place_type (city / town / village / hamlet),
        municipal_type, incorporated and population columns (see :func:`add_place_types`).
    """
    result = _lookup("city", "city", "city", state, county, None, listing_type, with_counts or place_type, proxy,
                     fallback_limit)
    if not place_type:
        return result
    result.insert(0, "state", get_state(state)["code"])
    return add_place_types(result, overpass_url=overpass_url)


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


# ---------------------------------------------------------------------------
# City / town / village types (OpenStreetMap)
# ---------------------------------------------------------------------------

_PLACE_RANK = {"city": 0, "town": 1, "village": 2, "hamlet": 3, "suburb": 4, "locality": 5, "neighbourhood": 6,
               "isolated_dwelling": 7}
_NAMES_PER_QUERY = 40


def _name_variants(name: str) -> list[str]:
    name = " ".join(str(name).split())
    variants = [name, name.title()]
    for short, long in (("St. ", "Saint "), ("St ", "Saint "), ("Ft. ", "Fort "), ("Ft ", "Fort "),
                        ("Mt. ", "Mount "), ("Mt ", "Mount ")):
        if name.startswith(short):
            variants.append(long + name[len(short):])
        if name.startswith(long):
            variants.append("St. " + name[len(long):] if long == "Saint " else short + name[len(long):])
    return list(dict.fromkeys(variants))


def _place_query(state_code: str, names: list[str]) -> str:
    from .osm import ql_string

    places = "".join(f'node(area.st)["place"]["name"={ql_string(n)}];' for n in names)
    towns = "".join(
        f'relation(area.st)["boundary"="administrative"]["admin_level"="8"]["name"={ql_string(n)}];' for n in names
    )
    return (
        "[out:json][timeout:120];\n"
        f'area["ISO3166-2"="US-{state_code}"]["admin_level"="4"]->.st;\n'
        f"({places})->.places;\n({towns})->.towns;\n"
        ".places out body;\n.towns out tags center;"
    )


def _osm_places_for_state(state_code: str, cities: dict[str, tuple[float, float] | None], client) -> dict[str, dict]:
    from .osm import haversine_m, osm_url

    variants = {city: _name_variants(city) for city in cities}
    all_names = list(dict.fromkeys(n for names in variants.values() for n in names))
    elements = []
    for start in range(0, len(all_names), _NAMES_PER_QUERY):
        data = client.query(_place_query(state_code, all_names[start:start + _NAMES_PER_QUERY]))
        elements += [e for e in data.get("elements", []) if isinstance(e, dict)]

    def coords(element):
        if element.get("lat") is not None:
            return element["lat"], element["lon"]
        center = element.get("center") or {}
        return (center["lat"], center["lon"]) if center.get("lat") is not None else None

    def near(point, element):
        here = coords(element)
        return haversine_m(point[0], point[1], here[0], here[1]) if point and here else 0.0

    out = {}
    for city, point in cities.items():
        keys = {n.lower() for n in variants[city]}
        nodes = [e for e in elements if e.get("type") == "node" and (e.get("tags") or {}).get("name", "").lower() in keys]
        bounds = [e for e in elements if e.get("type") == "relation"
                  and (e.get("tags") or {}).get("name", "").lower() in keys]
        nodes.sort(key=lambda e: (near(point, e), _PLACE_RANK.get(e["tags"].get("place"), 99)))
        node = nodes[0] if nodes else None
        anchor = coords(node) if node else point
        bounds.sort(key=lambda e: near(anchor, e))
        boundary = bounds[0] if bounds and (anchor is None or near(anchor, bounds[0]) <= 30_000) else None

        tags = (node or {}).get("tags") or {}
        population = str(tags.get("population") or (boundary or {}).get("tags", {}).get("population") or "")
        population = population.replace(",", "").split(";")[0].strip()
        out[city] = {
            "place_type": tags.get("place"),
            "municipal_type": ((boundary or {}).get("tags") or {}).get("border_type"),
            "incorporated": True if boundary else (False if node else None),
            "population": int(population) if population.isdigit() else None,
            "place_osm_url": osm_url(node or boundary) if (node or boundary) else None,
        }
    return out


def add_place_types(data: Any, city_column: str = "city", state_column: str = "state",
                    overpass_url: str | None = None, client=None) -> pd.DataFrame:
    """Add whether each city is a city, town, village or hamlet, using OpenStreetMap.

    Works on any DataFrame with city and state columns: scrape_property() results,
    get_cities(with_counts=True), aggregate_locations(), get_location_hierarchy(), ...

    Adds:
    * place_type - OpenStreetMap's settlement class (city / town / village / hamlet / ...), by size;
    * municipal_type - legal type of the municipality's boundary (city / town / village / borough), if any;
    * incorporated - True if the place has its own municipal boundary, False for unincorporated places
      such as census-designated places (e.g. Lehigh Acres, FL), None if not found;
    * population and place_osm_url.

    One Overpass query per state (per 40 names); answers are cached for 7 days.
    """
    from .nearby import get_overpass_client

    df = data.copy() if isinstance(data, pd.DataFrame) else pd.DataFrame(data)
    columns = ["place_type", "municipal_type", "incorporated", "population", "place_osm_url"]
    if df.empty or city_column not in df.columns or state_column not in df.columns:
        for column in columns:
            df[column] = pd.Series(dtype="object")
        return df

    client = client or get_overpass_client(overpass_url)
    has_coords = {"latitude", "longitude"} <= set(df.columns)
    wanted: dict[str, dict[str, tuple[float, float] | None]] = {}
    for (state, city), group in df.groupby([state_column, city_column], dropna=True):
        try:
            code = get_state(state)["code"]
        except ValueError:
            continue
        point = None
        if has_coords:
            lat = pd.to_numeric(group["latitude"], errors="coerce").mean()
            lon = pd.to_numeric(group["longitude"], errors="coerce").mean()
            point = (lat, lon) if pd.notna(lat) and pd.notna(lon) else None
        wanted.setdefault(code, {})[str(city)] = point

    found: dict[tuple[str, str], dict] = {}
    for code, cities in wanted.items():
        for city, info in _osm_places_for_state(code, cities, client).items():
            found[(code, city)] = info

    def lookup(row, column):
        try:
            code = get_state(row[state_column])["code"]
        except (ValueError, TypeError):
            return None
        return (found.get((code, str(row[city_column]))) or {}).get(column)

    for column in columns:
        df[column] = [lookup(row, column) for _, row in df.iterrows()]
    df["population"] = pd.array(df["population"], dtype="Int64")
    df["incorporated"] = pd.array(df["incorporated"], dtype="boolean")  # True / False / <NA> (not found)
    return df
