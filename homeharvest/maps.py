"""
homeharvest.maps
~~~~~~~~~~~~~~~~

Export scraped properties to interactive maps and map files.

Every exporter accepts the DataFrame returned by ``scrape_property()`` (or a list of
``Property`` models / raw dicts). Each point carries the listing's details and a link to the
listing on Realtor.com, plus Google Maps and OpenStreetMap links.

=====================  ==========================================================
Function               Output
=====================  ==========================================================
``to_osm_map``         Standalone HTML map (Leaflet + OpenStreetMap tiles)
``to_google_maps``     Standalone HTML map (Google Maps JavaScript API, needs a key)
``to_kml``             KML for Google Earth / Google My Maps / QGIS / ArcGIS
``to_google_my_maps_csv``  CSV ready for import into Google My Maps (2,000 rows/layer)
``to_geojson``         GeoJSON FeatureCollection
``export_map``         Picks the exporter from the file extension
=====================  ==========================================================
"""

from __future__ import annotations

import csv
import html
import io
import json
import math
import os
from datetime import date, datetime
from typing import Any, Iterable
from urllib.parse import quote_plus
from xml.sax.saxutils import escape as xml_escape

import pandas as pd

__all__ = [
    "to_osm_map",
    "to_google_maps",
    "to_kml",
    "to_google_my_maps_csv",
    "to_geojson",
    "export_map",
    "generate_map",
    "cluster_properties",
    "property_points",
]

LEAFLET_VERSION = "1.9.4"

HIGHLIGHT_COLOR = "#d50000"

STATUS_COLORS = {
    "FOR_SALE": "#2e7d32",
    "PENDING": "#ef6c00",
    "CONTINGENT": "#f9a825",
    "SOLD": "#616161",
    "FOR_RENT": "#1565c0",
    "OFF_MARKET": "#6d4c41",
}
DEFAULT_COLOR = "#7b1fa2"

_ALL_GROUPS = ["features", "property_history", "environmental_risk", "insurance", "market_stats"]


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (list, dict, tuple)):
        return False
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _clean(value: Any) -> Any:
    """Make a value JSON/XML friendly (NaN/NA -> None, numpy -> python, timestamps -> str)."""
    if _is_missing(value):
        return None
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if hasattr(value, "item") and not isinstance(value, (list, dict, str)):
        try:
            return value.item()
        except (ValueError, AttributeError):
            pass
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _records(properties: Any, matcher=None) -> list[dict]:
    """Turn a DataFrame / list[Property] / list[raw dict] into flat row dicts.

    With a FeatureMatcher, each item is matched on its original form (so pydantic/raw inputs can
    use feature details) and rows get matched_features / matched_keywords / feature_evidence
    plus a private ``_passes`` flag.
    """
    if properties is None:
        return []

    def annotate(row: dict, item: Any) -> dict:
        if matcher is not None and matcher.active:
            passes, result = matcher.match(item)
            row["matched_features"] = ", ".join(result.features) if passes and result.features else None
            row["matched_keywords"] = ", ".join(result.keywords) if passes and result.keywords else None
            row["feature_evidence"] = (
                "; ".join(f"{k}: {v}" for k, v in result.evidence.items()) if passes and result.evidence else None
            )
            row["_passes"] = passes
        else:
            row["_passes"] = bool(row.get("matched_features") or row.get("matched_keywords"))
        return row

    if isinstance(properties, pd.DataFrame):
        rows = []
        for raw_row in properties.to_dict(orient="records"):
            row = {k: _clean(v) for k, v in raw_row.items()}
            rows.append(annotate(row, row))
        return rows

    from .utils import process_result
    from .core.scrapers.models import Property

    rows = []
    for item in properties:
        if isinstance(item, Property):
            prop = item
        elif isinstance(item, dict) and isinstance(item.get("location"), dict):
            prop = _raw_to_property(item)
            if prop is None:
                continue
        elif isinstance(item, dict):
            row = {k: _clean(v) for k, v in item.items()}
            rows.append(annotate(row, row))
            continue
        else:
            raise TypeError(f"Unsupported property type: {type(item).__name__}")
        df = process_result(prop, _ALL_GROUPS)
        row = {k: _clean(v) for k, v in df.iloc[0].to_dict().items()}
        rows.append(annotate(row, item))
    return rows


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


class _GridIndex:
    """Bucket points into ~radius-sized cells for fast "anything within X miles?" lookups."""

    def __init__(self, radius_miles: float, ref_lat: float):
        self.radius = radius_miles
        self.dlat = radius_miles / 69.0
        self.dlon = radius_miles / max(1e-6, 69.172 * math.cos(math.radians(ref_lat)))
        self.cells: dict[tuple[int, int], list] = {}

    def _cell(self, lat: float, lon: float) -> tuple[int, int]:
        return int(math.floor(lat / self.dlat)), int(math.floor(lon / self.dlon))

    def add(self, lat: float, lon: float, item: Any) -> None:
        self.cells.setdefault(self._cell(lat, lon), []).append((lat, lon, item))

    def nearest(self, lat: float, lon: float) -> tuple[float, Any] | None:
        ci, cj = self._cell(lat, lon)
        best = None
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for plat, plon, item in self.cells.get((ci + di, cj + dj), ()):
                    distance = _haversine_miles(lat, lon, plat, plon)
                    if distance <= self.radius and (best is None or distance < best[0]):
                        best = (distance, item)
        return best


def _raw_to_property(raw: dict):
    from .core.scrapers.realtor.processors import process_property, process_extra_property_details, get_key
    from .core.scrapers.realtor.details import apply_details_to_property

    try:
        prop = process_property(raw, False, False, False, None, get_key, process_extra_property_details)
    except (KeyError, TypeError, AttributeError):
        return None
    if prop is None:
        return None
    apply_details_to_property(prop, raw, {"property_history", "environmental_risk", "insurance", "market_stats"})
    for name in ("matched_features", "matched_keywords", "feature_evidence"):
        if raw.get(name):
            setattr(prop, name, raw[name])
    return prop


def _money(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return f"${float(value):,.0f}"
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> str | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return f"{number:,.0f}" if number.is_integer() else f"{number:,.1f}"


def _safe_url(value: Any) -> str | None:
    if not value:
        return None
    url = str(value)
    return url if url.startswith(("https://", "http://")) else None


def _split(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v]
    return [v.strip() for v in str(value).split(",") if v.strip()]


def property_points(properties: Any, *, features=None, nearby=None, keywords=None, match_mode: str = "any",
                    only_matching: bool = False, near_matches_miles: float | None = None,
                    highlight_color: str = HIGHLIGHT_COLOR) -> list[dict]:
    """Normalize properties into map points (one dict per property with valid coordinates).

    :param features, nearby, keywords, match_mode: Highlight listings matching these (see
        scrape_property). Without them, listings already annotated by scrape_property(features=...)
        are highlighted.
    :param only_matching: Keep only the matching listings.
    :param near_matches_miles: Keep the matches plus listings within this many miles of a match
        (e.g. homes near a cul-de-sac listing); each nearby point records its distance.

    Each point has: id, lat, lon, title, address, url, price, price_label, status, color,
    status_color, is_match, matched (names), evidence, near_match_miles, facts (list of
    (label, value)), summary, photo, google_maps_url, osm_url and the original flat ``row``.
    """
    from .features import FeatureMatcher

    matcher = FeatureMatcher(features, nearby, keywords, match_mode)
    points = []
    for row in _records(properties, matcher):
        lat, lon = row.get("latitude"), row.get("longitude")
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0) or math.isnan(lat + lon):
            continue

        status = str(row.get("status") or "").upper() or None
        price = row.get("list_price") if row.get("list_price") is not None else row.get("sold_price")
        if status == "SOLD" and row.get("sold_price") is not None:
            price = row.get("sold_price")
        price_label = _money(price)
        if price_label and status == "FOR_RENT":
            price_label += "/mo"

        street = row.get("full_street_line") or row.get("street")
        address = row.get("formatted_address") or ", ".join(
            str(p) for p in (street, row.get("city"), row.get("state"), row.get("zip_code")) if p
        )

        baths = None
        if row.get("full_baths") is not None or row.get("half_baths") is not None:
            baths = (row.get("full_baths") or 0) + 0.5 * (row.get("half_baths") or 0)

        lot_acres = None
        if row.get("lot_sqft"):
            lot_acres = round(float(row["lot_sqft"]) / 43560, 2)

        facts = [
            ("Status", status.replace("_", " ").title() if status else None),
            ("Type", str(row["style"]).replace("_", " ").title() if row.get("style") else None),
            ("Beds", _number(row.get("beds"))),
            ("Baths", _number(baths)),
            ("Sqft", _number(row.get("sqft"))),
            ("Lot", f"{_number(row.get('lot_sqft'))} sqft ({lot_acres} ac)" if lot_acres is not None else None),
            ("Year built", row.get("year_built")),
            ("Days on market", row.get("days_on_mls")),
            ("Listed", str(row["list_date"])[:10] if row.get("list_date") else None),
            ("Sold", f"{str(row['last_sold_date'])[:10]} for {_money(row.get('sold_price')) or 'n/a'}"
             if row.get("last_sold_date") else None),
            ("Price/sqft", _money(row.get("price_per_sqft"))),
            ("HOA", f"{_money(row.get('hoa_fee'))}/mo" if row.get("hoa_fee") else None),
            ("Est. value", _money(row.get("estimated_value"))),
            ("Original price", _money(row.get("original_list_price"))),
            ("Flood risk", _risk_label(row, "flood")),
            ("Fire risk", _risk_label(row, "fire")),
            ("Heat risk", _risk_label(row, "heat")),
            ("Wind risk", _risk_label(row, "wind")),
            ("Air quality", _risk_label(row, "air")),
            ("FEMA zone", row.get("flood_fema_zone")),
            ("Home insurance", f"{_money(row.get('home_insurance_monthly'))}/mo"
             if row.get("home_insurance_monthly") else None),
            ("Rent estimate", f"{_money(row.get('estimated_rent'))}/mo" if row.get("estimated_rent") else None),
            ("County", row.get("county")),
            ("MLS #", row.get("mls_id")),
            ("Agent", row.get("agent_name")),
            ("Broker", row.get("broker_name") or row.get("office_name")),
        ]
        facts = [(label, value) for label, value in facts if value not in (None, "", "nan")]

        maps_query = quote_plus(f"{lat},{lon}")
        is_match = bool(row.pop("_passes", False))
        status_color = STATUS_COLORS.get(status, DEFAULT_COLOR)
        points.append({
            "id": row.get("property_id"),
            "lat": lat,
            "lon": lon,
            "title": f"{price_label} · {street}" if price_label and street else (street or address or price_label),
            "address": address,
            "url": _safe_url(row.get("property_url")),
            "price": price,
            "price_label": price_label,
            "status": status,
            "color": highlight_color if is_match else status_color,
            "status_color": status_color,
            "is_match": is_match,
            "near_match_miles": None,
            "facts": facts,
            "matched": _split(row.get("matched_features")) + _split(row.get("matched_keywords")),
            "evidence": row.get("feature_evidence"),
            "summary": _excerpt(row.get("text")),
            "photo": _safe_url(row.get("primary_photo")),
            "google_maps_url": f"https://www.google.com/maps/search/?api=1&query={maps_query}",
            "osm_url": f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=18/{lat}/{lon}",
            "row": row,
        })

    if only_matching:
        return [p for p in points if p["is_match"]]
    if near_matches_miles is not None:
        return _keep_near_matches(points, float(near_matches_miles))
    return points


def _keep_near_matches(points: list[dict], radius_miles: float) -> list[dict]:
    matches = [p for p in points if p["is_match"]]
    if not matches:
        return []
    index = _GridIndex(radius_miles, sum(p["lat"] for p in matches) / len(matches))
    for match in matches:
        index.add(match["lat"], match["lon"], match)

    kept = []
    for point in points:
        if point["is_match"]:
            kept.append(point)
            continue
        nearest = index.nearest(point["lat"], point["lon"])
        if nearest is None:
            continue
        distance, match = nearest
        point["near_match_miles"] = round(distance, 2)
        label = ", ".join(m.replace("_", " ") for m in match["matched"]) or "match"
        point["facts"].insert(0, ("Near match", f"{distance:.2f} mi from {match['address'] or match['title']} ({label})"))
        kept.append(point)
    return kept


def _excerpt(text: Any, limit: int = 220) -> str | None:
    if not text:
        return None
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def _risk_label(row: dict, prefix: str) -> str | None:
    severity, score = row.get(f"{prefix}_factor_severity"), row.get(f"{prefix}_factor_score")
    if severity is None and score is None:
        return None
    return f"{severity or ''} ({score}/10)".strip() if score is not None else str(severity)


# ---------------------------------------------------------------------------
# Popup / description HTML (all values escaped)
# ---------------------------------------------------------------------------

def _popup_html(point: dict) -> str:
    e = html.escape
    parts = ['<div class="hh-popup" style="font:13px/1.4 system-ui,sans-serif;max-width:300px">']
    if point["photo"]:
        parts.append(
            f'<img src="{e(point["photo"])}" alt="" loading="lazy" '
            f'style="width:100%;max-height:170px;object-fit:cover;border-radius:6px;margin-bottom:6px">'
        )
    if point["price_label"]:
        parts.append(f'<div style="font-size:16px;font-weight:700">{e(point["price_label"])}</div>')
    if point["address"]:
        parts.append(f'<div style="margin-bottom:6px">{e(point["address"])}</div>')
    if point["matched"]:
        badges = "".join(
            f'<span style="display:inline-block;background:#d50000;color:#fff;border-radius:10px;'
            f'padding:1px 8px;margin:0 4px 4px 0;font-size:11px">{e(m.replace("_", " "))}</span>'
            for m in point["matched"]
        )
        parts.append(f"<div>{badges}</div>")
        if point["evidence"]:
            parts.append(f'<div style="color:#555;font-size:11px;margin-bottom:6px">{e(str(point["evidence"]))}</div>')
    if point.get("summary"):
        parts.append(f'<div style="color:#333;font-size:12px;margin-bottom:6px">{e(point["summary"])}</div>')
    if point["facts"]:
        rows = "".join(
            f'<tr><td style="color:#666;padding-right:8px;white-space:nowrap">{e(str(label))}</td>'
            f"<td>{e(str(value))}</td></tr>"
            for label, value in point["facts"]
        )
        parts.append(f'<table style="border-collapse:collapse;font-size:12px">{rows}</table>')
    links = []
    if point["url"]:
        links.append(f'<a href="{e(point["url"])}" target="_blank" rel="noopener">View on Realtor.com</a>')
    links.append(f'<a href="{e(point["google_maps_url"])}" target="_blank" rel="noopener">Google Maps</a>')
    links.append(f'<a href="{e(point["osm_url"])}" target="_blank" rel="noopener">OpenStreetMap</a>')
    parts.append(f'<div style="margin-top:6px">{" · ".join(links)}</div>')
    parts.append("</div>")
    return "".join(parts)


def _json_for_script(data: Any) -> str:
    """JSON safe to embed inside a <script> tag."""
    return (
        json.dumps(data, ensure_ascii=False, default=str)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def _write(path: str | os.PathLike | None, content: str, newline: str | None = None) -> None:
    if path is None:
        return
    directory = os.path.dirname(os.fspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline=newline) as f:
        f.write(content)


def _legend(points: list[dict], highlight_color: str = HIGHLIGHT_COLOR) -> list[dict]:
    legend = []
    if any(p["is_match"] for p in points):
        legend.append({"label": "★ Matches", "color": highlight_color})
    seen = set()
    for point in points:
        status = point["status"] or "OTHER"
        if status not in seen:
            seen.add(status)
            legend.append({"label": status.replace("_", " ").title(), "color": point["status_color"]})
    if any(p["near_match_miles"] is not None for p in points):
        legend.append({"label": "(non-matches shown are near a match)", "color": "transparent"})
    return legend


def _match_options(kwargs: dict) -> dict:
    """Pop the shared matching options from an exporter's kwargs."""
    return {
        key: kwargs.pop(key)
        for key in ("features", "nearby", "keywords", "match_mode", "only_matching", "near_matches_miles",
                    "highlight_color")
        if key in kwargs
    }


# ---------------------------------------------------------------------------
# Spatial clustering
# ---------------------------------------------------------------------------

def _cluster_ids(coords: list[tuple[float, float]], radius_miles: float) -> list[int]:
    """Greedy leader clustering: each point joins the nearest cluster seed within radius_miles."""
    if not coords:
        return []
    index = _GridIndex(radius_miles, sum(lat for lat, _ in coords) / len(coords))
    ids, next_id = [], 1
    for lat, lon in coords:
        nearest = index.nearest(lat, lon)
        if nearest is None:  # no seed within radius: this point seeds a new cluster
            index.add(lat, lon, next_id)
            ids.append(next_id)
            next_id += 1
        else:
            ids.append(nearest[1])
    return ids


def cluster_properties(properties: Any, radius_miles: float = 0.5) -> pd.DataFrame:
    """Group properties into spatial clusters (all members within ``radius_miles`` of the seed).

    Returns a DataFrame (from scrape_property() or converted from models/raw dicts) with
    cluster_id, cluster_size, cluster_center_lat and cluster_center_lon columns. Rows without
    coordinates get no cluster.
    """
    if radius_miles <= 0:
        raise ValueError("radius_miles must be positive")
    df = properties.copy() if isinstance(properties, pd.DataFrame) else pd.DataFrame(
        [{k: v for k, v in r.items() if k != "_passes"} for r in _records(properties)])
    if df.empty:
        for column in ("cluster_id", "cluster_size", "cluster_center_lat", "cluster_center_lon"):
            df[column] = pd.Series(dtype="float64")
        return df

    lat = pd.to_numeric(df.get("latitude"), errors="coerce")
    lon = pd.to_numeric(df.get("longitude"), errors="coerce")
    valid = lat.notna() & lon.notna()
    ids = _cluster_ids(list(zip(lat[valid], lon[valid])), radius_miles)

    df["cluster_id"] = pd.Series(pd.NA, index=df.index, dtype="Int64")
    df.loc[valid, "cluster_id"] = ids
    groups = df.loc[valid].assign(_lat=lat[valid], _lon=lon[valid]).groupby("cluster_id")
    df["cluster_size"] = df["cluster_id"].map(groups.size()).astype("Int64")
    df["cluster_center_lat"] = df["cluster_id"].map(groups["_lat"].mean())
    df["cluster_center_lon"] = df["cluster_id"].map(groups["_lon"].mean())
    return df


# ---------------------------------------------------------------------------
# GeoJSON
# ---------------------------------------------------------------------------

def to_geojson(properties: Any, path: str | os.PathLike | None = None, **match_options) -> dict:
    """Export properties as a GeoJSON FeatureCollection (and write it to ``path`` if given).

    Accepts the matching options of :func:`property_points` (features, nearby, keywords,
    match_mode, only_matching, near_matches_miles); features get is_match / near_match_miles.
    """
    features = []
    for point in property_points(properties, **_match_options(match_options)):
        props = {k: v for k, v in point["row"].items() if k not in ("latitude", "longitude")}
        props.update({
            "title": point["title"],
            "realtor_url": point["url"],
            "google_maps_url": point["google_maps_url"],
            "osm_url": point["osm_url"],
            "marker_color": point["color"],
            "is_match": point["is_match"],
            "matched": point["matched"] or None,
            "near_match_miles": point["near_match_miles"],
        })
        features.append({
            "type": "Feature",
            "id": point["id"],
            "geometry": {"type": "Point", "coordinates": [point["lon"], point["lat"]]},
            "properties": props,
        })
    _reject_unknown(match_options, "to_geojson")
    collection = {"type": "FeatureCollection", "features": features}
    if path is not None:
        _write(path, json.dumps(collection, ensure_ascii=False, default=str, indent=2))
    return collection


def _reject_unknown(kwargs: dict, func: str) -> None:
    if kwargs:
        raise TypeError(f"{func}() got unexpected keyword argument(s): {', '.join(sorted(kwargs))}")


# ---------------------------------------------------------------------------
# KML (Google Earth / Google My Maps)
# ---------------------------------------------------------------------------

def _kml_color(hex_color: str, alpha: str = "ff") -> str:
    """'#rrggbb' -> KML 'aabbggrr'."""
    h = hex_color.lstrip("#")
    return f"{alpha}{h[4:6]}{h[2:4]}{h[0:2]}"


_KML_FIELDS = [
    ("property_id", "Property ID"), ("status", "Status"), ("list_price", "List price"),
    ("sold_price", "Sold price"), ("beds", "Beds"), ("full_baths", "Full baths"), ("sqft", "Sqft"),
    ("lot_sqft", "Lot sqft"), ("year_built", "Year built"), ("style", "Type"), ("days_on_mls", "Days on market"),
    ("list_date", "List date"), ("mls_id", "MLS #"), ("county", "County"), ("agent_name", "Agent"),
    ("matched_features", "Matched features"), ("matched_keywords", "Matched keywords"),
    ("flood_factor_severity", "Flood risk"), ("fire_factor_severity", "Fire risk"),
    ("property_url", "Realtor.com"),
]


def _kml_placemark(point: dict, style_id: str) -> str:
    cdata = _popup_html(point).replace("]]>", "]]]]><![CDATA[>")
    extended = "".join(
        f'<Data name="{xml_escape(label)}"><value>{xml_escape(str(point["row"].get(key)))}</value></Data>'
        for key, label in _KML_FIELDS
        if point["row"].get(key) is not None
    )
    if point["near_match_miles"] is not None:
        extended += f'<Data name="Miles to nearest match"><value>{point["near_match_miles"]}</value></Data>'
    extended += f'<Data name="Google Maps"><value>{xml_escape(point["google_maps_url"])}</value></Data>'
    name = ("★ " if point["is_match"] else "") + (point["title"] or "")
    return (
        "<Placemark>"
        f"<name>{xml_escape(name)}</name>"
        f"<Snippet maxLines=\"1\">{xml_escape(point['address'] or '')}</Snippet>"
        f"<description><![CDATA[{cdata}]]></description>"
        f"<styleUrl>#{style_id}</styleUrl>"
        f"<ExtendedData>{extended}</ExtendedData>"
        f"<Point><coordinates>{point['lon']},{point['lat']},0</coordinates></Point>"
        "</Placemark>"
    )


def _kml_style(style_id: str, color: str, scale: float = 1.0, icon: str = "wht-blank") -> str:
    return (
        f'<Style id="{style_id}"><IconStyle><color>{_kml_color(color)}</color><scale>{scale}</scale>'
        f"<Icon><href>https://maps.google.com/mapfiles/kml/paddle/{icon}.png</href></Icon></IconStyle>"
        '<BalloonStyle><text>$[description]</text></BalloonStyle></Style>'
    )


KML_CLUSTER_BY = (None, "status", "match", "zip_code", "city", "county", "proximity")


def to_kml(properties: Any, path: str | os.PathLike | None = None, *, name: str = "HomeHarvest properties",
           cluster_by: str | None = "status", cluster_radius_miles: float = 0.5, **match_options) -> str:
    """Export properties as KML (Google Earth, Google My Maps, QGIS, ArcGIS...).

    Each placemark has an HTML balloon with the listing details and a link to Realtor.com, plus the
    key fields as ExtendedData (shown as a data table in Google My Maps). Matches (see
    ``features``/``nearby``/``keywords``) use a red star icon and a ★ in their name.

    :param cluster_by: How placemarks are grouped into folders: "status" (default), "match"
        (matches vs others), "zip_code", "city", "county", "proximity" (spatial clusters of
        ``cluster_radius_miles``) or None (no folders). KML has no marker clustering of its own,
        so folders are how Google Earth/My Maps users expand and collapse groups.
    """
    if cluster_by not in KML_CLUSTER_BY:
        raise ValueError(f"cluster_by must be one of {KML_CLUSTER_BY}, got {cluster_by!r}")
    options = _match_options(match_options)
    _reject_unknown(match_options, "to_kml")
    points = property_points(properties, **options)

    styles = {"match": _kml_style("match", options.get("highlight_color", HIGHLIGHT_COLOR), 1.3, "red-stars")}
    for point in points:
        status = point["status"] or "OTHER"
        style_id = f"status-{status.lower()}"
        styles.setdefault(style_id, _kml_style(style_id, point["status_color"]))

    def style_of(point):
        return "match" if point["is_match"] else f"status-{(point['status'] or 'OTHER').lower()}"

    if cluster_by == "proximity":
        ids = _cluster_ids([(p["lat"], p["lon"]) for p in points], cluster_radius_miles)
    folders: dict[str, list[str]] = {}
    for i, point in enumerate(points):
        if cluster_by is None:
            folder = ""
        elif cluster_by == "status":
            folder = "★ Matches" if point["is_match"] else (point["status"] or "OTHER").replace("_", " ").title()
        elif cluster_by == "match":
            folder = "★ Matches" if point["is_match"] else "Other properties"
        elif cluster_by == "proximity":
            folder = f"Cluster {ids[i]}"
        else:
            folder = str(point["row"].get(cluster_by) or "Unknown")
        folders.setdefault(folder, []).append(_kml_placemark(point, style_of(point)))

    if cluster_by == "proximity":
        def describe(folder: str, marks: list[str]) -> str:
            members = [p for p, cid in zip(points, ids) if f"Cluster {cid}" == folder]
            places = sorted({str(p["row"].get("city") or "") for p in members} - {""})
            hits = sum(p["is_match"] for p in members)
            return f"{folder} · {len(marks)} properties{' · ' + ', '.join(places) if places else ''}" + (
                f" · {hits} ★" if hits else "")
    else:
        def describe(folder: str, marks: list[str]) -> str:
            return f"{folder} ({len(marks)})"

    ordered = sorted(folders.items(), key=lambda kv: (not kv[0].startswith("★"),)) if cluster_by else folders.items()
    body = "".join(
        "".join(marks) if not folder else
        f"<Folder><name>{xml_escape(describe(folder, marks))}</name>{''.join(marks)}</Folder>"
        for folder, marks in ordered
    )
    kml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        f"<name>{xml_escape(name)}</name>"
        f"{''.join(styles.values())}{body}"
        "</Document></kml>\n"
    )
    _write(path, kml)
    return kml


# ---------------------------------------------------------------------------
# Google My Maps CSV
# ---------------------------------------------------------------------------

MY_MAPS_MAX_ROWS = 2000


def to_google_my_maps_csv(properties: Any, path: str | os.PathLike | None = None,
                          max_rows_per_file: int = MY_MAPS_MAX_ROWS, **match_options) -> list[str]:
    """Export a CSV that Google My Maps can import (Create map -> Import).

    My Maps imports at most 2,000 rows per layer, so larger exports are split into
    ``name_part1.csv``, ``name_part2.csv``, ... Returns the CSV text(s); written to disk if ``path``.
    In My Maps, use "Style by data column: Highlighted" to color the matches.
    """
    options = _match_options(match_options)
    _reject_unknown(match_options, "to_google_my_maps_csv")
    columns = ["Name", "Latitude", "Longitude", "Address", "Price", "Status", "Beds", "Baths", "Sqft",
               "Lot acres", "Year built", "Highlighted", "Matched", "Miles to nearest match",
               "Realtor.com link", "Google Maps link", "Description"]
    rows = []
    for point in property_points(properties, **options):
        row = point["row"]
        baths = dict(point["facts"]).get("Baths")
        lot = round(float(row["lot_sqft"]) / 43560, 2) if row.get("lot_sqft") else None
        description = "; ".join(f"{label}: {value}" for label, value in point["facts"])
        rows.append([
            point["title"], point["lat"], point["lon"], point["address"], point["price_label"],
            point["status"], row.get("beds"), baths, row.get("sqft"), lot, row.get("year_built"),
            "yes" if point["is_match"] else "no", ", ".join(point["matched"]) or None, point["near_match_miles"],
            point["url"], point["google_maps_url"], description,
        ])

    chunks = [rows[i:i + max_rows_per_file] for i in range(0, len(rows), max_rows_per_file)] or [[]]
    texts = []
    for index, chunk in enumerate(chunks, start=1):
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(columns)
        writer.writerows(["" if v is None else v for v in r] for r in chunk)
        texts.append(buffer.getvalue())
        if path is not None:
            target = os.fspath(path)
            if len(chunks) > 1:
                stem, ext = os.path.splitext(target)
                target = f"{stem}_part{index}{ext or '.csv'}"
            _write(target, buffer.getvalue(), newline="")
    return texts


# ---------------------------------------------------------------------------
# Interactive HTML maps
# ---------------------------------------------------------------------------

MARKERCLUSTER_VERSION = "1.5.3"
GOOGLE_MARKERCLUSTERER_VERSION = "2.5.3"


def _map_payload(points: list[dict]) -> list[dict]:
    return [
        {
            "lat": p["lat"], "lon": p["lon"], "title": p["title"] or "", "color": p["color"],
            "status": (p["status"] or "OTHER").replace("_", " ").title(),
            "popup": _popup_html(p), "is_match": p["is_match"],
        }
        for p in points
    ]


_PAGE_CSS = """
html,body{margin:0;height:100%;background:#fff}
#map{position:absolute;inset:0}
.hh-legend{background:#fff;padding:8px 10px;border-radius:6px;box-shadow:0 1px 4px rgba(0,0,0,.3);
  font:12px/1.5 system-ui,sans-serif;color:#222;max-width:260px}
.hh-legend i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px}
.hh-title{font-weight:700;margin-bottom:4px}
.hh-pin{border-radius:50%;border:2px solid #fff;box-shadow:0 0 3px rgba(0,0,0,.6);box-sizing:border-box}
.hh-pin.hh-hit{border-color:#fff;box-shadow:0 0 0 2px #d50000,0 0 6px rgba(0,0,0,.6)}
.hh-cluster-hit{background:rgba(213,0,0,.25);border-radius:50%;width:40px;height:40px;display:flex;
  align-items:center;justify-content:center}
.hh-cluster-hit span{background:#d50000;color:#fff;border-radius:50%;width:30px;height:30px;display:flex;
  align-items:center;justify-content:center;font:bold 12px system-ui,sans-serif}
"""


def to_osm_map(properties: Any, path: str | os.PathLike | None = None, *, title: str = "HomeHarvest properties",
               cluster: bool = True, cluster_radius: int = 60, disable_clustering_at_zoom: int | None = 17,
               cluster_matches: bool = False,
               tiles: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
               attribution: str = "&copy; <a href=\"https://www.openstreetmap.org/copyright\">OpenStreetMap</a> contributors",
               **match_options) -> str:
    """Interactive HTML map using Leaflet and OpenStreetMap tiles (no API key needed).

    :param cluster: Group nearby markers into clusters that split apart as you zoom in.
    :param cluster_radius: Max cluster radius in pixels (smaller = more, tighter clusters).
    :param disable_clustering_at_zoom: Show every marker from this zoom level on (None = always cluster).
    :param cluster_matches: Also cluster the highlighted matches (default: always show them).
    :param features, nearby, keywords, match_mode, only_matching, near_matches_miles: Highlight
        matching listings in red (own "★ Matches" layer); see :func:`property_points`.

    Each popup shows the listing details, photo and links to Realtor.com, Google Maps and
    OpenStreetMap. Layers can be toggled per status in the layer control.
    """
    options = _match_options(match_options)
    _reject_unknown(match_options, "to_osm_map")
    points = property_points(properties, **options)
    config = {
        "title": title, "tiles": tiles, "attribution": attribution, "legend": _legend(points),
        "cluster": bool(cluster), "clusterRadius": int(cluster_radius),
        "disableAt": disable_clustering_at_zoom, "clusterMatches": bool(cluster_matches),
    }
    cluster_assets = ""
    if cluster:
        base = f"https://unpkg.com/leaflet.markercluster@{MARKERCLUSTER_VERSION}/dist"
        cluster_assets = (
            f'<link rel="stylesheet" href="{base}/MarkerCluster.css" crossorigin="">\n'
            f'<link rel="stylesheet" href="{base}/MarkerCluster.Default.css" crossorigin="">\n'
            f'<script src="{base}/leaflet.markercluster.js" crossorigin=""></script>'
        )
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@{LEAFLET_VERSION}/dist/leaflet.css" crossorigin="">
<style>{_PAGE_CSS}</style>
</head>
<body>
<div id="map"></div>
<script src="https://unpkg.com/leaflet@{LEAFLET_VERSION}/dist/leaflet.js" crossorigin=""></script>
{cluster_assets}
<script>
const POINTS = {_json_for_script(_map_payload(points))};
const CONFIG = {_json_for_script(config)};
const MATCHES = '★ Matches';
const map = L.map('map');
L.tileLayer(CONFIG.tiles, {{maxZoom: 19, attribution: CONFIG.attribution}}).addTo(map);

function pin(color, hit) {{
  const size = hit ? 20 : 14;
  return L.divIcon({{
    className: '', iconSize: [size, size], iconAnchor: [size / 2, size / 2], popupAnchor: [0, -size / 2],
    html: '<div class="hh-pin' + (hit ? ' hh-hit' : '') + '" style="width:' + size + 'px;height:' + size +
          'px;background:' + color + '"></div>'
  }});
}}

function clusterGroup(hit) {{
  const options = {{maxClusterRadius: CONFIG.clusterRadius, showCoverageOnHover: false, chunkedLoading: true}};
  if (CONFIG.disableAt !== null) options.disableClusteringAtZoom = CONFIG.disableAt;
  if (hit) options.iconCreateFunction = c => L.divIcon({{
    className: '', iconSize: [40, 40],
    html: '<div class="hh-cluster-hit"><span>' + c.getChildCount() + '</span></div>'}});
  return L.markerClusterGroup(options);
}}

// One marker list per layer name (matches first, then each status)
const groups = {{}};
const bounds = [];
for (const p of POINTS) {{
  const name = p.is_match ? MATCHES : p.status;
  const marker = L.marker([p.lat, p.lon], {{icon: pin(p.color, p.is_match), title: p.title,
                                            zIndexOffset: p.is_match ? 1000 : 0}})
    .bindPopup(p.popup, {{maxWidth: 320}});
  (groups[name] = groups[name] || []).push(marker);
  bounds.push([p.lat, p.lon]);
}}

const shared = CONFIG.cluster ? clusterGroup(false).addTo(map) : null;
const matchCluster = CONFIG.cluster && CONFIG.clusterMatches ? clusterGroup(true).addTo(map) : null;

function toggle(target, markers) {{
  // An empty layer whose visibility adds/removes its markers from a shared cluster group
  const layer = L.layerGroup();
  layer.on('add', () => target.addLayers(markers));
  layer.on('remove', () => target.removeLayers(markers));
  return layer;
}}

const overlays = {{}};
const names = Object.keys(groups).sort((a, b) => (b === MATCHES) - (a === MATCHES));
for (const name of names) {{
  const markers = groups[name];
  let layer;
  if (name === MATCHES) layer = matchCluster ? toggle(matchCluster, markers) : L.layerGroup(markers);
  else layer = shared ? toggle(shared, markers) : L.layerGroup(markers);
  layer.addTo(map);
  overlays[name + ' (' + markers.length + ')'] = layer;
}}
L.control.layers(null, overlays, {{collapsed: false}}).addTo(map);

const legend = L.control({{position: 'bottomleft'}});
legend.onAdd = () => {{
  const div = L.DomUtil.create('div', 'hh-legend');
  const head = L.DomUtil.create('div', 'hh-title', div);
  head.textContent = CONFIG.title + ' (' + POINTS.length + ')';
  for (const l of CONFIG.legend) {{
    const row = L.DomUtil.create('div', '', div);
    const dot = L.DomUtil.create('i', '', row);
    dot.style.background = l.color;
    row.appendChild(document.createTextNode(l.label));
  }}
  return div;
}};
legend.addTo(map);

if (bounds.length) {{ map.fitBounds(bounds, {{padding: [30, 30], maxZoom: 16}}); }}
else {{ map.setView([39.5, -98.35], 4); }}
</script>
</body>
</html>
"""
    _write(path, page)
    return page


def to_google_maps(properties: Any, path: str | os.PathLike | None = None, *, api_key: str,
                   title: str = "HomeHarvest properties", cluster: bool = True, cluster_matches: bool = False,
                   **match_options) -> str:
    """Interactive HTML map using the Google Maps JavaScript API.

    Requires a Google Maps JavaScript API key (https://developers.google.com/maps/documentation/javascript/get-api-key).
    Without a key, use :func:`to_kml` or :func:`to_google_my_maps_csv` and import the file into
    Google My Maps (mymaps.google.com) - every point also links to Google Maps.

    :param cluster: Cluster markers with @googlemaps/markerclusterer.
    :param cluster_matches: Also cluster the highlighted matches (default: always show them).
    """
    if not api_key:
        raise ValueError(
            "to_google_maps() needs a Google Maps JavaScript API key. Without one, export with "
            "to_kml() or to_google_my_maps_csv() and import the file into Google My Maps."
        )
    options = _match_options(match_options)
    _reject_unknown(match_options, "to_google_maps")
    points = property_points(properties, **options)
    config = {"title": title, "legend": _legend(points), "cluster": bool(cluster),
              "clusterMatches": bool(cluster_matches)}
    clusterer = (
        f'<script src="https://unpkg.com/@googlemaps/markerclusterer@{GOOGLE_MARKERCLUSTERER_VERSION}'
        f'/dist/index.min.js" crossorigin=""></script>' if cluster else ""
    )
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{_PAGE_CSS}</style>
</head>
<body>
<div id="map"></div>
{clusterer}
<script>
const POINTS = {_json_for_script(_map_payload(points))};
const CONFIG = {_json_for_script(config)};
function initMap() {{
  const map = new google.maps.Map(document.getElementById('map'), {{
    center: {{lat: 39.5, lng: -98.35}}, zoom: 4, mapTypeControl: true, streetViewControl: true}});
  const info = new google.maps.InfoWindow();
  const bounds = new google.maps.LatLngBounds();
  const plain = [], hits = [];
  for (const p of POINTS) {{
    const marker = new google.maps.Marker({{
      position: {{lat: p.lat, lng: p.lon}}, title: p.title, zIndex: p.is_match ? 1000 : 1,
      icon: {{path: google.maps.SymbolPath.CIRCLE, scale: p.is_match ? 9 : 6, fillColor: p.color,
              fillOpacity: 1, strokeColor: '#fff', strokeWeight: 2}}
    }});
    marker.addListener('click', () => {{ info.setContent(p.popup); info.open({{map, anchor: marker}}); }});
    bounds.extend(marker.getPosition());
    (p.is_match ? hits : plain).push(marker);
  }}
  const canCluster = CONFIG.cluster && window.markerClusterer;
  if (canCluster) new markerClusterer.MarkerClusterer({{map, markers: plain}});
  else plain.forEach(m => m.setMap(map));
  if (canCluster && CONFIG.clusterMatches) new markerClusterer.MarkerClusterer({{map, markers: hits}});
  else hits.forEach(m => m.setMap(map));
  if (POINTS.length) map.fitBounds(bounds);

  const legend = document.createElement('div');
  legend.className = 'hh-legend';
  legend.style.margin = '10px';
  const head = document.createElement('div');
  head.className = 'hh-title';
  head.textContent = CONFIG.title + ' (' + POINTS.length + ')';
  legend.appendChild(head);
  CONFIG.legend.forEach(l => {{
    const row = document.createElement('div');
    const dot = document.createElement('i');
    dot.style.background = l.color;
    row.appendChild(dot);
    row.appendChild(document.createTextNode(l.label));
    legend.appendChild(row);
  }});
  map.controls[google.maps.ControlPosition.LEFT_BOTTOM].push(legend);
}}
</script>
<script async src="https://maps.googleapis.com/maps/api/js?key={quote_plus(api_key)}&callback=initMap"></script>
</body>
</html>
"""
    _write(path, page)
    return page


# ---------------------------------------------------------------------------
# Dispatcher + one-call scrape & map
# ---------------------------------------------------------------------------

_FORMATS = {
    ".html": "osm", ".htm": "osm", ".kml": "kml", ".geojson": "geojson", ".json": "geojson", ".csv": "my_maps_csv",
}


def export_map(properties: Any, path: str | os.PathLike, format: str | None = None, **kwargs) -> str:
    """Export to a map file, choosing the format from ``format`` or the file extension.

    Formats: ``osm`` (.html), ``google`` (.html, needs ``api_key=``), ``kml`` (.kml),
    ``geojson`` (.geojson/.json), ``my_maps_csv`` (.csv). Extra keyword arguments go to the
    exporter (e.g. cluster=True, features=["cul_de_sac"], near_matches_miles=0.5). Returns the path.
    """
    fmt = (format or _FORMATS.get(os.path.splitext(os.fspath(path))[1].lower()) or "").lower()
    if fmt in ("osm", "openstreetmap", "leaflet", "html"):
        to_osm_map(properties, path, **kwargs)
    elif fmt in ("google", "google_maps"):
        to_google_maps(properties, path, **kwargs)
    elif fmt == "kml":
        to_kml(properties, path, **kwargs)
    elif fmt == "geojson":
        to_geojson(properties, path, **kwargs)
    elif fmt in ("my_maps_csv", "csv", "google_my_maps"):
        to_google_my_maps_csv(properties, path, **kwargs)
    else:
        raise ValueError(
            f"Unknown map format {format or os.path.splitext(os.fspath(path))[1]!r}. "
            "Use one of: osm (.html), google, kml (.kml), geojson (.geojson), my_maps_csv (.csv)."
        )
    return os.fspath(path)


_MAP_ONLY_OPTIONS = ("title", "cluster", "cluster_radius", "disable_clustering_at_zoom", "cluster_matches",
                     "tiles", "attribution", "api_key", "name", "cluster_by", "cluster_radius_miles",
                     "max_rows_per_file", "highlight_color")


def generate_map(location: str, path: str | os.PathLike, *, format: str | None = None,
                 features=None, nearby=None, keywords=None, match_mode: str = "any",
                 only_matching: bool = False, near_matches_miles: float | None = None,
                 cluster: bool = True, title: str | None = None, **kwargs) -> pd.DataFrame:
    """Scrape a location and write a map in one call. Returns the scraped DataFrame.

    Every listing is fetched and the ones matching ``features`` / ``nearby`` / ``keywords`` are
    highlighted (``only_matching=True`` drops the rest; ``near_matches_miles=0.5`` keeps the
    matches plus listings within half a mile of one). Other keyword arguments go to
    :func:`homeharvest.scrape_property` (listing_type, property_type, past_days,
    include_environmental_risk, ...) or to the exporter (cluster_radius, api_key, cluster_by, ...).

        >>> generate_map("Lehigh Acres, FL", "lehigh.html", property_type=["land"],
        ...              features=["cul_de_sac"], near_matches_miles=0.25)
    """
    from . import scrape_property

    map_kwargs = {k: kwargs.pop(k) for k in list(kwargs) if k in _MAP_ONLY_OPTIONS}
    fmt = (format or _FORMATS.get(os.path.splitext(os.fspath(path))[1].lower()) or "").lower()
    if fmt in ("osm", "openstreetmap", "leaflet", "html", "google", "google_maps"):
        map_kwargs["cluster"] = cluster

    # With only_matching, filter during the scrape; otherwise fetch everything and highlight on the map.
    scrape_filters = dict(features=features, nearby=nearby, keywords=keywords, match_mode=match_mode)
    df = scrape_property(location=location, **(scrape_filters if only_matching else {}), **kwargs)

    if fmt not in ("my_maps_csv", "csv", "google_my_maps", "geojson"):
        map_kwargs.setdefault("title" if fmt != "kml" else "name", title or f"{location} ({len(df)} properties)")
    export_map(df, path, format=format, **scrape_filters, only_matching=only_matching,
               near_matches_miles=near_matches_miles, **map_kwargs)
    return df
