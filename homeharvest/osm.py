"""
homeharvest.osm
~~~~~~~~~~~~~~~

OpenStreetMap helpers: a polite Overpass API client and distance geometry.

The public Overpass servers are shared and rate-limited, so the client sends at most one query
per second (shared across threads), retries overloaded/timeouts with backoff, honors
``Retry-After`` and caches answers on disk for 7 days.

Environment variables:

* ``HOMEHARVEST_OVERPASS_URL`` - another Overpass endpoint (e.g. your own server or a mirror).
* ``HOMEHARVEST_CACHE`` - cache directory; ``off`` disables the cache.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import tempfile
import time
from pathlib import Path
from typing import Iterable

import requests

from .core.client import limiter_for
from .exceptions import OverpassError

DEFAULT_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
#: Generic, identifies the library only (no personal details are sent).
DEFAULT_USER_AGENT = "HomeHarvest (Python real estate library; OpenStreetMap proximity lookups)"
CACHE_TTL_SECONDS = 7 * 24 * 3600
FEET_PER_METER = 3.28084


def default_cache_dir() -> Path | None:
    value = os.environ.get("HOMEHARVEST_CACHE")
    if value is not None and value.strip().lower() in ("", "0", "off", "false", "no", "none"):
        return None
    base = Path(value).expanduser() if value else Path.home() / ".cache" / "homeharvest"
    return base / "overpass"


class OverpassClient:
    """Send Overpass QL queries politely and cache the JSON answers."""

    def __init__(self, url: str | None = None, *, cache_dir: Path | str | None | bool = True,
                 cache_ttl: float = CACHE_TTL_SECONDS, queries_per_second: float = 1.0, max_attempts: int = 6,
                 timeout: float = 180.0, user_agent: str | None = None, session: requests.Session | None = None,
                 sleep=None):
        self.url = url or os.environ.get("HOMEHARVEST_OVERPASS_URL") or DEFAULT_OVERPASS_URL
        if cache_dir is True:
            cache_dir = default_cache_dir()
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.cache_ttl = cache_ttl
        self.queries_per_second = queries_per_second
        self.max_attempts = max_attempts
        self.timeout = timeout
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.session = session
        self._sleep = sleep
        self.limiter = limiter_for(self.url)
        self.http_requests = 0

    def sleep(self, seconds: float) -> None:
        (self._sleep or time.sleep)(seconds)

    # -- cache -----------------------------------------------------------------
    def _cache_path(self, ql: str) -> Path | None:
        if not self.cache_dir:
            return None
        digest = hashlib.sha256(f"{self.url}\n{ql}".encode()).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def _cache_get(self, ql: str) -> dict | None:
        path = self._cache_path(ql)
        if not path or not path.exists():
            return None
        if time.time() - path.stat().st_mtime > self.cache_ttl:
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None  # corrupt cache entry: ignore it

    def _cache_put(self, ql: str, data: dict) -> None:
        path = self._cache_path(ql)
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, path)
        except OSError:
            pass  # caching is best effort

    # -- query -------------------------------------------------------------------
    def query(self, ql: str) -> dict:
        cached = self._cache_get(ql)
        if cached is not None:
            return cached

        session = self.session or requests.Session()
        last_error = "no attempt made"
        for attempt in range(1, self.max_attempts + 1):
            self.limiter.wait(self.queries_per_second)
            retry_after = None
            try:
                self.http_requests += 1
                response = session.post(self.url, data={"data": ql}, headers={"User-Agent": self.user_agent},
                                        timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = f"network error: {exc}"
            else:
                status = response.status_code
                if status == 200:
                    try:
                        data = response.json()
                    except ValueError:
                        last_error = "answer was not JSON"
                    else:
                        remark = str(data.get("remark") or "")
                        if "runtime error" in remark.lower():
                            last_error = f"Overpass runtime error: {remark[:200]}"
                        else:
                            self._cache_put(ql, data)
                            return data
                elif status == 400:
                    raise ValueError(f"Overpass rejected the query (HTTP 400): {response.text[:300]}")
                else:
                    last_error = f"HTTP {status}"
                    try:
                        retry_after = float(response.headers.get("Retry-After"))
                    except (TypeError, ValueError):
                        retry_after = None
            if attempt < self.max_attempts:
                delay = min(300.0, 10.0 * 2 ** (attempt - 1)) * random.uniform(0.8, 1.2)
                if retry_after is not None:
                    delay = max(delay, min(retry_after, 600.0))
                self.sleep(delay)
        raise OverpassError(f"Overpass query failed after {self.max_attempts} attempts ({last_error}). "
                            "The public server may be busy; try again later or set overpass_url.")


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


class _Projection:
    """Local equirectangular projection (meters) around a point; accurate to well under 1% at ~50 km."""

    def __init__(self, lat0: float, lon0: float):
        self.lat0, self.lon0 = lat0, lon0
        self.kx = 111_320.0 * math.cos(math.radians(lat0))
        self.ky = 110_574.0

    def xy(self, lat: float, lon: float) -> tuple[float, float]:
        return (lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky


def _segment_distance(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _inside(px, py, ring: list[tuple[float, float]]) -> bool:
    inside = False
    for i in range(len(ring) - 1):
        (x1, y1), (x2, y2) = ring[i], ring[i + 1]
        if (y1 > py) != (y2 > py) and px < (x2 - x1) * (py - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _coords(geometry: Iterable[dict] | None) -> list[tuple[float, float]]:
    return [(p["lat"], p["lon"]) for p in geometry or [] if p and p.get("lat") is not None]


def element_lines(element: dict) -> tuple[list[list[tuple[float, float]]], list[tuple[float, float]], list, list]:
    """Split an Overpass element into (lines, points, outer_rings, inner_rings) in lat/lon."""
    kind = element.get("type")
    lines, points, outers, inners = [], [], [], []
    if kind == "node":
        if element.get("lat") is not None:
            points.append((element["lat"], element["lon"]))
    elif kind == "way":
        coords = _coords(element.get("geometry"))
        if coords:
            lines.append(coords)
            if len(coords) >= 4 and coords[0] == coords[-1]:
                outers.append(coords)
    elif kind == "relation":
        for member in element.get("members") or []:
            if member.get("type") == "node" and member.get("lat") is not None:
                points.append((member["lat"], member["lon"]))
            coords = _coords(member.get("geometry"))
            if not coords:
                continue
            lines.append(coords)
            if len(coords) >= 4 and coords[0] == coords[-1]:
                (inners if member.get("role") == "inner" else outers).append(coords)
    if not lines and not points and element.get("center"):
        points.append((element["center"]["lat"], element["center"]["lon"]))
    return lines, points, outers, inners


def distance_to_element_m(lat: float, lon: float, element: dict, area: bool = True) -> float | None:
    """Meters from a point to an OSM element (0 if inside an area feature such as a park or lake)."""
    lines, points, outers, inners = element_lines(element)
    if not lines and not points:
        return None
    proj = _Projection(lat, lon)
    best = math.inf
    for plat, plon in points:
        best = min(best, haversine_m(lat, lon, plat, plon))
    projected_lines = [[proj.xy(a, b) for a, b in line] for line in lines]
    for line in projected_lines:
        if len(line) == 1:
            best = min(best, math.hypot(*line[0]))
        for (ax, ay), (bx, by) in zip(line, line[1:]):
            best = min(best, _segment_distance(0.0, 0.0, ax, ay, bx, by))
    if area and outers:
        in_outer = any(_inside(0.0, 0.0, [proj.xy(a, b) for a, b in ring]) for ring in outers)
        in_inner = any(_inside(0.0, 0.0, [proj.xy(a, b) for a, b in ring]) for ring in inners)
        if in_outer and not in_inner:
            return 0.0
    return best


def element_bbox(element: dict) -> tuple[float, float, float, float] | None:
    """(min_lat, min_lon, max_lat, max_lon) of an element, for quick pruning."""
    lines, points, _, _ = element_lines(element)
    coords = points + [c for line in lines for c in line]
    if not coords:
        return None
    lats, lons = [c[0] for c in coords], [c[1] for c in coords]
    return min(lats), min(lons), max(lats), max(lons)


def bbox_distance_m(lat: float, lon: float, bbox: tuple[float, float, float, float]) -> float:
    """Lower bound of the distance from a point to anything inside a bbox (meters)."""
    min_lat, min_lon, max_lat, max_lon = bbox
    clat = min(max(lat, min_lat), max_lat)
    clon = min(max(lon, min_lon), max_lon)
    return haversine_m(lat, lon, clat, clon) * 0.99


def osm_url(element: dict) -> str | None:
    if element.get("type") in ("node", "way", "relation") and element.get("id") is not None:
        return f"https://www.openstreetmap.org/{element['type']}/{element['id']}"
    return None


def ql_string(value: str) -> str:
    """Quote a string for Overpass QL."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'
