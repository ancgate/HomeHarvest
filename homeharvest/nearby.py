"""
homeharvest.nearby
~~~~~~~~~~~~~~~~~~

Real distances from properties to nearby OpenStreetMap features: cul-de-sacs, water, paved or
dirt roads, schools, power lines, ...

    >>> scrape_property("Lehigh Acres, FL", property_type=["land"],
    ...                 near={"cul_de_sac": "300 ft"}, far_from={"power_line": "500 ft"})
    >>> add_nearby(df, ["water", "school", "grocery"])     # adds water_ft, water_name, school_ft, ...

Unlike the text-based ``features=`` / ``nearby=`` filters, these measure geography: a lot 109 ft
from a cul-de-sac turning circle matches even if its listing never mentions it. Accuracy depends
on how completely OpenStreetMap maps the area.

Distances are in feet. A number means feet; strings may use ft, mi, m, km or yd ("0.5 mi").
"""

from __future__ import annotations

import difflib
import re
import warnings
from dataclasses import dataclass
from typing import Any, Iterable

from .osm import (
    FEET_PER_METER,
    OverpassClient,
    bbox_distance_m,
    distance_to_element_m,
    element_bbox,
    osm_url,
    ql_string,
)

__all__ = ["ASPECTS", "list_aspects", "parse_distance", "add_nearby", "find_nearby", "normalize_proximity"]


# ---------------------------------------------------------------------------
# Aspect catalog
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Selector:
    """OSM element types + tag filters. Renders to Overpass QL and matches returned elements."""

    types: tuple[str, ...]
    filters: tuple[tuple[str, str, str | None], ...]  # (key, op, value); op: "=", "~", "exists", "!exists"

    def render(self) -> list[str]:
        parts = []
        for key, op, value in self.filters:
            if op == "=":
                parts.append(f"[{ql_string(key)}={ql_string(value)}]")
            elif op == "~":
                parts.append(f"[{ql_string(key)}~{ql_string(value)}]")
            elif op == "exists":
                parts.append(f"[{ql_string(key)}]")
            elif op == "!exists":
                parts.append(f"[!{ql_string(key)}]")
        return [f"{kind}{''.join(parts)}" for kind in self.types]

    def matches(self, element: dict) -> bool:
        if "nwr" not in self.types and element.get("type") not in self.types:
            return False
        tags = element.get("tags") or {}
        for key, op, value in self.filters:
            present = key in tags
            if op == "=" and tags.get(key) != value:
                return False
            if op == "~" and (not present or not re.search(value, str(tags[key]))):
                return False
            if op == "exists" and not present:
                return False
            if op == "!exists" and present:
                return False
        return True


def _sel(types: str, *filters: tuple[str, str, str | None]) -> Selector:
    return Selector(tuple(types.split(",")), tuple(filters))


@dataclass(frozen=True)
class Aspect:
    key: str
    label: str
    selectors: tuple[Selector, ...]
    default_radius_ft: float
    area: bool = True  # inside an area feature (park, lake...) counts as distance 0
    description: str = ""

    def matches(self, element: dict) -> bool:
        return any(sel.matches(element) for sel in self.selectors)


_ROADS = "^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street|service|track|road)$"
_PAVED_ROADS = "^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street|service)$"
_PAVED = "^(paved|asphalt|concrete|concrete:plates|concrete:lanes|paving_stones|chipseal|sett)$"
_UNPAVED = "^(unpaved|dirt|gravel|ground|earth|sand|compacted|fine_gravel|grass|mud|pebblestone)$"
_LAKE_WATER = "^(lake|reservoir|pond|lagoon|oxbow)$"

ASPECTS: dict[str, Aspect] = {a.key: a for a in (
    Aspect("cul_de_sac", "Cul-de-sac", (_sel("node", ("highway", "~", "^(turning_circle|turning_loop)$")),), 2640,
           description="Turning circle / loop at the end of a street"),
    Aspect("dead_end", "Dead end", (_sel("node", ("noexit", "=", "yes")),), 2640,
           description="Road end tagged noexit=yes"),
    Aspect("road", "Road", (_sel("way", ("highway", "~", _ROADS)),), 2640, area=False),
    Aspect("paved_road", "Paved road", (
        _sel("way", ("highway", "~", _PAVED_ROADS), ("surface", "~", _PAVED)),
        _sel("way", ("highway", "~", "^(motorway|trunk|primary|secondary|tertiary)$"), ("surface", "!exists", None)),
    ), 2640, area=False, description="Roads tagged with a paved surface, plus major roads without a surface tag"),
    Aspect("dirt_road", "Dirt/gravel road", (
        _sel("way", ("highway", "exists", None), ("surface", "~", _UNPAVED)),
        _sel("way", ("highway", "=", "track")),
    ), 2640, area=False),
    Aspect("major_road", "Major road", (_sel("way", ("highway", "~", "^(motorway|trunk|primary|secondary)$")),),
           10560, area=False),
    Aspect("freeway", "Freeway", (_sel("way", ("highway", "~", "^(motorway|trunk)$")),), 26400, area=False),
    Aspect("water", "Water", (
        _sel("way,relation", ("natural", "=", "water")),
        _sel("way", ("waterway", "~", "^(river|stream|canal)$")),
        _sel("way", ("natural", "=", "coastline")),
    ), 5280, description="Lakes, ponds, rivers, streams, canals and coastline"),
    Aspect("lake", "Lake/pond", (
        _sel("way,relation", ("natural", "=", "water"), ("water", "~", _LAKE_WATER)),
        _sel("way,relation", ("natural", "=", "water"), ("water", "!exists", None)),
    ), 5280),
    Aspect("river", "River/stream", (
        _sel("way", ("waterway", "~", "^(river|stream)$")),
        _sel("way,relation", ("natural", "=", "water"), ("water", "~", "^(river|stream)$")),
    ), 5280),
    Aspect("canal", "Canal", (
        _sel("way", ("waterway", "=", "canal")),
        _sel("way,relation", ("natural", "=", "water"), ("water", "=", "canal")),
    ), 5280),
    Aspect("coastline", "Coastline", (_sel("way", ("natural", "=", "coastline")),), 26400, area=False),
    Aspect("beach", "Beach", (_sel("nwr", ("natural", "=", "beach")),), 26400),
    Aspect("wetland", "Wetland", (_sel("way,relation", ("natural", "=", "wetland")),), 2640),
    Aspect("park", "Park", (
        _sel("nwr", ("leisure", "~", "^(park|nature_reserve)$")),
        _sel("way,relation", ("boundary", "=", "national_park")),
    ), 10560),
    Aspect("protected_area", "Protected / public land", (_sel("way,relation", ("boundary", "=", "protected_area")),),
           26400, description="State/national forests, wildlife management areas, preserves"),
    Aspect("forest", "Forest/woods", (_sel("way,relation", ("landuse", "=", "forest")),
                                      _sel("way,relation", ("natural", "=", "wood"))), 5280),
    Aspect("golf_course", "Golf course", (_sel("way,relation", ("leisure", "=", "golf_course")),), 10560),
    Aspect("school", "School", (_sel("nwr", ("amenity", "=", "school")),), 15840),
    Aspect("hospital", "Hospital", (_sel("nwr", ("amenity", "=", "hospital")),), 52800),
    Aspect("fire_station", "Fire station", (_sel("nwr", ("amenity", "=", "fire_station")),), 26400),
    Aspect("grocery", "Grocery store", (_sel("nwr", ("shop", "~", "^(supermarket|grocery)$")),), 26400),
    Aspect("gas_station", "Gas station", (_sel("nwr", ("amenity", "=", "fuel")),), 26400),
    Aspect("town", "Town", (_sel("node", ("place", "~", "^(city|town|village)$")),), 52800, area=False),
    Aspect("airport", "Airport", (_sel("nwr", ("aeroway", "=", "aerodrome")),), 79200),
    Aspect("railroad", "Railroad", (_sel("way", ("railway", "=", "rail")),), 10560, area=False),
    Aspect("power_line", "Power line", (_sel("way", ("power", "~", "^(line|minor_line)$")),), 5280, area=False),
    Aspect("substation", "Substation", (_sel("nwr", ("power", "=", "substation")),), 10560),
    Aspect("cell_tower", "Cell tower", (
        _sel("node", ("man_made", "~", "^(mast|tower)$"), ("tower:type", "=", "communication")),
        _sel("node", ("communication:mobile_phone", "=", "yes")),
    ), 10560),
    Aspect("landfill", "Landfill", (_sel("way,relation", ("landuse", "=", "landfill")),), 15840),
    Aspect("industrial", "Industrial area", (_sel("way,relation", ("landuse", "=", "industrial")),), 5280),
    Aspect("cemetery", "Cemetery", (_sel("way,relation", ("landuse", "=", "cemetery")),
                                    _sel("nwr", ("amenity", "=", "grave_yard"))), 5280),
    Aspect("boat_ramp", "Boat ramp", (_sel("nwr", ("leisure", "=", "slipway")),), 26400),
    Aspect("marina", "Marina", (_sel("nwr", ("leisure", "=", "marina")),), 26400),
)}

_ALIASES = {
    "culdesac": "cul_de_sac", "cul_de_sacs": "cul_de_sac", "turning_circle": "cul_de_sac",
    "deadend": "dead_end", "roads": "road", "paved": "paved_road", "dirt": "dirt_road", "gravel_road": "dirt_road",
    "highway": "major_road", "interstate": "freeway", "pond": "lake", "lakes": "lake", "stream": "river",
    "creek": "river", "ocean": "coastline", "coast": "coastline", "woods": "forest", "public_land": "protected_area",
    "wildlife_management_area": "protected_area", "schools": "school", "supermarket": "grocery",
    "power_lines": "power_line", "powerline": "power_line", "railway": "railroad", "rail": "railroad",
    "tower": "cell_tower", "dump": "landfill", "graveyard": "cemetery", "golf": "golf_course",
}


def list_aspects() -> list[dict]:
    """Built-in aspects with their default search radius (feet)."""
    return [{"key": a.key, "label": a.label, "default_radius_ft": a.default_radius_ft, "description": a.description}
            for a in ASPECTS.values()]


def _raw_tag_aspect(spec: str) -> Aspect | None:
    """'leisure=marina', 'sport~tennis|pickleball' or 'amenity=*' -> a custom aspect over any element type."""
    match = re.fullmatch(r"\s*([A-Za-z0-9_:.\-]+)\s*(=|~)\s*(.+?)\s*", spec)
    if not match:
        return None
    key, op, value = match.groups()
    selector = _sel("nwr", (key, "exists", None)) if (op, value) == ("=", "*") else _sel("nwr", (key, op, value))
    column = re.sub(r"[^a-z0-9]+", "_", spec.strip().lower()).strip("_")
    return Aspect(column, spec.strip(), (selector,), 5280, description=f"Custom OSM tag {spec.strip()}")


def resolve_aspect(name: str) -> Aspect:
    key = re.sub(r"[\s\-]+", "_", str(name).strip().lower())
    key = _ALIASES.get(key, key)
    if key in ASPECTS:
        return ASPECTS[key]
    custom = _raw_tag_aspect(str(name))
    if custom:
        return custom
    hint = difflib.get_close_matches(key, list(ASPECTS) + list(_ALIASES), n=3)
    raise ValueError(
        f"Unknown nearby aspect {name!r}.{' Did you mean: ' + ', '.join(hint) + '?' if hint else ''} "
        "Use homeharvest.list_aspects(), or a raw OpenStreetMap tag such as 'leisure=marina'."
    )


# ---------------------------------------------------------------------------
# Distances
# ---------------------------------------------------------------------------

_UNITS = {"ft": 1.0, "feet": 1.0, "foot": 1.0, "'": 1.0, "mi": 5280.0, "mile": 5280.0, "miles": 5280.0,
          "m": FEET_PER_METER, "meter": FEET_PER_METER, "meters": FEET_PER_METER, "km": 1000 * FEET_PER_METER,
          "yd": 3.0, "yard": 3.0, "yards": 3.0}


def parse_distance(value: Any) -> float:
    """Distance in feet from a number (feet) or a string like '300 ft', '0.5 mi', '200 m', '1 km'."""
    if isinstance(value, bool):
        raise ValueError(f"Invalid distance {value!r}")
    if isinstance(value, (int, float)):
        feet = float(value)
    else:
        match = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*([A-Za-z']*)\s*", str(value))
        if not match or match.group(2).lower() not in _UNITS and match.group(2):
            raise ValueError(f"Invalid distance {value!r}. Use a number of feet or e.g. '300 ft', '0.5 mi', '200 m'.")
        feet = float(match.group(1)) * _UNITS.get(match.group(2).lower() or "ft", 1.0)
    if feet <= 0:
        raise ValueError(f"Distance must be positive, got {value!r}")
    return feet


def normalize_proximity(spec: Any, param: str = "near") -> dict[str, tuple[Aspect, float]]:
    """{column: (aspect, feet)} from a dict {aspect: distance}, a list of aspects or one aspect name."""
    if spec is None:
        return {}
    if isinstance(spec, str):
        spec = [spec]
    items = spec.items() if isinstance(spec, dict) else [(name, None) for name in spec]
    out = {}
    for name, distance in items:
        try:
            aspect = resolve_aspect(name)
        except ValueError as exc:
            raise ValueError(f"{param}: {exc}") from None
        out[aspect.key] = (aspect, parse_distance(distance) if distance is not None else aspect.default_radius_ft)
    return out


def format_distance(feet: float) -> str:
    return f"{feet / 5280:.1f} mi" if feet >= 5280 else f"{feet:,.0f} ft"


# ---------------------------------------------------------------------------
# Querying
# ---------------------------------------------------------------------------

MAX_STATEMENTS = 200
MAX_POINTS_PER_QUERY = 80


def get_overpass_client(overpass_url: str | None = None) -> OverpassClient:
    """The Overpass client used by scrape_property(near=..., far_from=...). Replaceable in tests."""
    return OverpassClient(overpass_url)


def _client(overpass_url=None, client=None):
    return client or get_overpass_client(overpass_url)


def build_query(points: list[tuple[float, float]], aspects: list[tuple[Aspect, float]], timeout: int = 120) -> str:
    """Overpass QL: every selector of every aspect around every point (radius in meters)."""
    statements = []
    for lat, lon in points:
        for aspect, feet in aspects:
            meters = feet / FEET_PER_METER
            for selector in aspect.selectors:
                for rendered in selector.render():
                    statements.append(f"{rendered}(around:{meters:.0f},{lat:.6f},{lon:.6f});")
    return f"[out:json][timeout:{timeout}];\n(\n" + "\n".join(statements) + "\n);\nout geom;"


def find_nearby(points: list[tuple[float, float] | None], aspects: dict[str, tuple[Aspect, float]],
                overpass_url: str | None = None, client=None) -> list[dict[str, dict]]:
    """For each (lat, lon) point, the nearest feature of each aspect within its radius.

    Returns one dict per point: {aspect: {"distance_ft", "name", "osm_type", "osm_id", "osm_url"}};
    distance_ft is None when nothing was found within the radius. Points that are None get {}.
    """
    results: list[dict[str, dict]] = [{} for _ in points]
    valid = [(i, p) for i, p in enumerate(points) if p is not None]
    if not valid or not aspects:
        return results

    client = _client(overpass_url, client)
    aspect_list = list(aspects.values())
    per_point = sum(len(sel.render()) for aspect, _ in aspect_list for sel in aspect.selectors)
    batch_size = max(1, min(MAX_POINTS_PER_QUERY, MAX_STATEMENTS // max(1, per_point)))

    for start in range(0, len(valid), batch_size):
        batch = valid[start:start + batch_size]
        data = client.query(build_query([p for _, p in batch], aspect_list))
        elements = [e for e in data.get("elements", []) if isinstance(e, dict)]
        boxes = [element_bbox(e) for e in elements]
        for index, (lat, lon) in batch:
            for column, (aspect, feet) in aspects.items():
                radius_m = feet / FEET_PER_METER
                best = None
                for element, box in zip(elements, boxes):
                    if box is None or not aspect.matches(element):
                        continue
                    if bbox_distance_m(lat, lon, box) > radius_m:
                        continue
                    distance = distance_to_element_m(lat, lon, element, area=aspect.area)
                    if distance is not None and distance <= radius_m * 1.001 and (best is None or distance < best[0]):
                        best = (distance, element)
                if best is None:
                    results[index][column] = {"distance_ft": None, "name": None, "osm_type": None,
                                              "osm_id": None, "osm_url": None}
                else:
                    tags = best[1].get("tags") or {}
                    results[index][column] = {
                        "distance_ft": int(round(best[0] * FEET_PER_METER)),
                        "name": tags.get("name") or tags.get("ref"),
                        "osm_type": best[1].get("type"),
                        "osm_id": best[1].get("id"),
                        "osm_url": osm_url(best[1]),
                    }
    return results


# ---------------------------------------------------------------------------
# Listing helpers (Property models, raw dicts, DataFrames)
# ---------------------------------------------------------------------------

def _coordinates(listing: Any) -> tuple[float, float] | None:
    if isinstance(listing, dict):
        lat, lon = listing.get("latitude"), listing.get("longitude")
        if lat is None and isinstance(listing.get("location"), dict):
            coord = ((listing["location"].get("address") or {}).get("coordinate")) or {}
            lat, lon = coord.get("lat"), coord.get("lon")
    else:
        lat, lon = getattr(listing, "latitude", None), getattr(listing, "longitude", None)
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if lat != lat or lon != lon or (lat == 0 and lon == 0):
        return None
    return lat, lon


def _store(listing: Any, found: dict[str, dict]) -> None:
    if isinstance(listing, dict):
        listing["nearby"] = {**(listing.get("nearby") or {}), **found}
        return
    from .core.scrapers.models import NearbyFeature
    merged = dict(listing.nearby or {})
    merged.update({k: NearbyFeature(**v) for k, v in found.items()})
    listing.nearby = merged


def apply_proximity(listings: list, near=None, far_from=None, overpass_url: str | None = None, client=None) -> list:
    """Measure distances for near/far_from aspects, store them on each listing and filter.

    near: every aspect must be within its distance. far_from: no feature of any aspect within its distance.
    Listings without coordinates are dropped (they can't be checked).
    """
    near_spec = normalize_proximity(near, "near")
    far_spec = normalize_proximity(far_from, "far_from")
    if not near_spec and not far_spec:
        return listings

    query_spec = dict(near_spec)
    for column, (aspect, feet) in far_spec.items():
        if column not in query_spec or query_spec[column][1] < feet:
            query_spec[column] = (aspect, feet)

    points = [_coordinates(listing) for listing in listings]
    found = find_nearby(points, query_spec, overpass_url=overpass_url, client=client)

    kept, missing = [], 0
    for listing, point, features in zip(listings, points, found):
        if point is None:
            missing += 1
            continue
        _store(listing, features)
        ok = all((features[c]["distance_ft"] is not None and features[c]["distance_ft"] <= feet)
                 for c, (_, feet) in near_spec.items())
        ok = ok and all((features[c]["distance_ft"] is None or features[c]["distance_ft"] > feet)
                        for c, (_, feet) in far_spec.items())
        if ok:
            kept.append(listing)
    if missing:
        warnings.warn(f"{missing} listing(s) without coordinates were dropped by the near/far_from filter.",
                      RuntimeWarning, stacklevel=2)
    return kept


def nearby_columns(spec: dict) -> list[str]:
    columns = []
    for column in spec:
        columns += [f"{column}_ft", f"{column}_name"]
    return columns


def add_nearby(properties: Any, aspects: Any, radius: Any = None, overpass_url: str | None = None, client=None):
    """Add the distance to (and name of) the nearest feature of each aspect. Never drops rows.

    :param properties: DataFrame from scrape_property(), or a list of Property models / raw dicts.
    :param aspects: list of aspect names (default radius each), or {aspect: max distance}.
    :param radius: one search radius for all aspects (e.g. "1 mi"), overriding the defaults.
    Returns the DataFrame with ``<aspect>_ft`` / ``<aspect>_name`` columns (feet; None when nothing
    is found within the radius), or the same list with ``nearby`` filled in.
    """
    spec = normalize_proximity(aspects, "aspects")
    if radius is not None:
        feet = parse_distance(radius)
        spec = {column: (aspect, feet) for column, (aspect, _) in spec.items()}

    try:
        import pandas as pd
    except ImportError:  # pragma: no cover
        pd = None

    if pd is not None and isinstance(properties, pd.DataFrame):
        df = properties.copy()
        rows = df.to_dict(orient="records")
        found = find_nearby([_coordinates(r) for r in rows], spec, overpass_url=overpass_url, client=client)
        for column in spec:
            df[f"{column}_ft"] = pd.array([f.get(column, {}).get("distance_ft") for f in found], dtype="Int64")
            df[f"{column}_name"] = [f.get(column, {}).get("name") for f in found]
        return df

    listings = list(properties)
    found = find_nearby([_coordinates(item) for item in listings], spec, overpass_url=overpass_url, client=client)
    for listing, features in zip(listings, found):
        if features:
            _store(listing, features)
    return listings

