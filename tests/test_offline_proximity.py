"""Offline tests for real distances to OpenStreetMap features (near / far_from / add_nearby)."""

import json
import warnings

import pandas as pd
import pytest

from homeharvest import add_nearby, list_aspects, scrape_property, to_osm_map
from homeharvest import nearby as nearby_module
from homeharvest.core.scrapers.realtor import RealtorScraper
from homeharvest.exceptions import OverpassError
from homeharvest.nearby import build_query, find_nearby, normalize_proximity, parse_distance
from homeharvest.osm import DEFAULT_USER_AGENT, OverpassClient, distance_to_element_m

from .fakes import FakeRealtorAPI, make_home

FT = 1 / 3.28084 / 111194.93  # degrees of latitude per foot (same Earth radius as the haversine formula)
LAT, LON = 26.6, -81.6

# Home 1 at (LAT, LON); cul-de-sac 100 ft north of it.
# Home 2 at (LAT, LON + 0.01); power line running north-south ~200 ft east of it.
# Home 3 inside a lake polygon around (LAT + 0.01, LON).
LON_FT = 1 / 3.28084 / (111320 * 0.8942)  # degrees of longitude per foot at 26.6N (cos 26.6 = 0.8942)
WORLD = [
    {"type": "node", "id": 1, "lat": LAT + 100 * FT, "lon": LON,
     "tags": {"highway": "turning_circle", "name": "Oak Ct"}},
    {"type": "way", "id": 2, "tags": {"power": "line"},
     "geometry": [{"lat": LAT - 0.01, "lon": LON + 0.01 + 200 * LON_FT},
                  {"lat": LAT + 0.01, "lon": LON + 0.01 + 200 * LON_FT}]},
    {"type": "way", "id": 3, "tags": {"natural": "water", "name": "Lake Lucy"},
     "geometry": [{"lat": LAT + 0.009, "lon": LON - 0.001}, {"lat": LAT + 0.009, "lon": LON + 0.001},
                  {"lat": LAT + 0.011, "lon": LON + 0.001}, {"lat": LAT + 0.011, "lon": LON - 0.001},
                  {"lat": LAT + 0.009, "lon": LON - 0.001}]},
    {"type": "node", "id": 4, "lat": LAT + 0.02, "lon": LON, "tags": {"amenity": "school", "name": "Lehigh Elementary"}},
]


class FakeOverpass:
    def __init__(self, elements=WORLD):
        self.elements = elements
        self.queries = []

    def query(self, ql):
        self.queries.append(ql)
        return {"elements": self.elements}


@pytest.fixture
def overpass(monkeypatch):
    fake = FakeOverpass()
    monkeypatch.setattr(nearby_module, "get_overpass_client", lambda url=None: fake)
    return fake


@pytest.fixture
def fake_api(monkeypatch):
    def install(**kwargs):
        api = FakeRealtorAPI(**kwargs)
        monkeypatch.setattr(RealtorScraper, "_graphql_post", lambda self, q, v, op: api(self, q, v, op))
        return api
    return install


HOMES = [
    make_home("1", lat=LAT, lon=LON),
    make_home("2", lat=LAT, lon=LON + 0.01),
    make_home("3", lat=LAT + 0.01, lon=LON),
    make_home("4", lat=None, lon=None),
]


# ---------------------------------------------------------------------------
# Geometry and parsing
# ---------------------------------------------------------------------------

def test_distances_to_node_line_and_polygon():
    assert distance_to_element_m(LAT, LON, WORLD[0]) * 3.28084 == pytest.approx(100, abs=1)
    assert distance_to_element_m(LAT, LON + 0.01, WORLD[1], area=False) * 3.28084 == pytest.approx(200, abs=3)
    assert distance_to_element_m(LAT + 0.01, LON, WORLD[2]) == 0.0           # inside the lake
    outside = distance_to_element_m(LAT + 0.01, LON + 0.002, WORLD[2])        # east of it
    assert outside == pytest.approx(0.001 * 111320 * 0.8942, rel=0.02)


def test_multipolygon_hole_is_not_inside():
    ring = lambda d: [{"lat": LAT - d, "lon": LON - d}, {"lat": LAT - d, "lon": LON + d},
                      {"lat": LAT + d, "lon": LON + d}, {"lat": LAT + d, "lon": LON - d}, {"lat": LAT - d, "lon": LON - d}]
    park = {"type": "relation", "id": 9, "tags": {"leisure": "park"},
            "members": [{"type": "way", "role": "outer", "geometry": ring(0.01)},
                        {"type": "way", "role": "inner", "geometry": ring(0.001)}]}
    assert distance_to_element_m(LAT + 0.005, LON, park) == 0.0
    assert distance_to_element_m(LAT, LON, park) == pytest.approx(0.001 * 110574, rel=0.12)  # to the hole's edge


@pytest.mark.parametrize("value,feet", [(300, 300), ("300 ft", 300), ("0.5 mi", 2640), ("100 m", 328.084),
                                        ("1 km", 3280.84), ("10 yd", 30)])
def test_parse_distance(value, feet):
    assert parse_distance(value) == pytest.approx(feet)


def test_bad_aspects_and_distances():
    with pytest.raises(ValueError, match="Did you mean: cul_de_sac"):
        normalize_proximity({"cul_de_sack": 100})
    with pytest.raises(ValueError, match="Invalid distance"):
        normalize_proximity({"water": "5 parsecs"})
    with pytest.raises(ValueError, match="near: Unknown nearby aspect"):
        scrape_property("Lehigh Acres, FL", near=["schol"])


def test_aliases_and_raw_tags():
    spec = normalize_proximity(["culdesac", "creek", "leisure=marina", "amenity=*"])
    assert list(spec) == ["cul_de_sac", "river", "leisure_marina", "amenity"]
    assert spec["amenity"][0].selectors[0].render() == ['nwr["amenity"]']
    assert {a["key"] for a in list_aspects()} >= {"cul_de_sac", "water", "paved_road", "power_line", "school"}


def test_query_text_and_batching():
    ql = build_query([(LAT, LON)], list(normalize_proximity({"cul_de_sac": "300 ft"}).values()))
    assert ql.startswith("[out:json][timeout:120];")
    assert 'node["highway"~"^(turning_circle|turning_loop)$"](around:91,26.600000,-81.600000);' in ql
    assert ql.rstrip().endswith("out geom;")

    fake = FakeOverpass(elements=[])
    find_nearby([(LAT, LON)] * 100, normalize_proximity(["cul_de_sac"]), client=fake)
    assert len(fake.queries) == 2                    # 80 points per query
    fake = FakeOverpass(elements=[])
    find_nearby([(LAT, LON)] * 100, normalize_proximity(["water"]), client=fake)  # 4 statements per point
    assert len(fake.queries) == 2 and fake.queries[0].count("(around:") == 200


# ---------------------------------------------------------------------------
# scrape_property(near=..., far_from=...)
# ---------------------------------------------------------------------------

def test_near_filter_keeps_close_listings_and_adds_columns(fake_api, overpass):
    fake_api(homes=HOMES)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        df = scrape_property("Lehigh Acres, FL", near={"cul_de_sac": "300 ft"})
    assert list(df["property_id"]) == ["1"]
    assert df.iloc[0]["cul_de_sac_ft"] == pytest.approx(100, abs=1)
    assert df.iloc[0]["cul_de_sac_name"] == "Oak Ct"
    assert any("without coordinates" in str(w.message) for w in caught)


def test_far_from_and_combined_filters(fake_api, overpass):
    fake_api(homes=HOMES[:3])
    df = scrape_property("Lehigh Acres, FL", far_from={"power_line": "500 ft"})
    assert list(df["property_id"]) == ["1", "3"]
    assert pd.isna(df.set_index("property_id").loc["1", "power_line_ft"])  # none within 500 ft

    # home 1 is ~0.6 mi from the lake: inside the default 1-mile radius, so ask for "on the water"
    assert set(scrape_property("Lehigh Acres, FL", near=["water"])["property_id"]) == {"1", "2", "3"}
    on_water = scrape_property("Lehigh Acres, FL", near={"water": "100 ft"}, far_from={"power_line": 500})
    assert list(on_water["property_id"]) == ["3"] and on_water.iloc[0]["water_ft"] == 0
    assert on_water.iloc[0]["water_name"] == "Lake Lucy"


def test_proximity_runs_after_text_filters(fake_api, overpass):
    homes = [make_home("1", lat=LAT, lon=LON, text="Quiet cul-de-sac"), make_home("2", lat=LAT, lon=LON, text="Lot")]
    fake_api(homes=homes)
    df = scrape_property("Lehigh Acres, FL", keywords=["cul de sac"], near={"cul_de_sac": 300})
    assert list(df["property_id"]) == ["1"]
    assert overpass.queries[0].count("(around:") == 1  # only the listing that passed the keyword filter


def test_proximity_on_pydantic_and_raw(fake_api, overpass):
    fake_api(homes=HOMES[:1])
    (prop,) = scrape_property("Lehigh Acres, FL", near=["cul_de_sac"], return_type="pydantic")
    assert prop.nearby["cul_de_sac"].name == "Oak Ct" and prop.nearby["cul_de_sac"].osm_url.endswith("/node/1")
    (raw,) = scrape_property("Lehigh Acres, FL", near=["cul_de_sac"], return_type="raw")
    assert raw["nearby"]["cul_de_sac"]["distance_ft"] == pytest.approx(100, abs=1)


# ---------------------------------------------------------------------------
# add_nearby and maps
# ---------------------------------------------------------------------------

def test_add_nearby_dataframe_keeps_every_row(fake_api, overpass):
    fake_api(homes=HOMES)
    df = scrape_property("Lehigh Acres, FL")
    out = add_nearby(df, ["school", "cul_de_sac"], client=overpass)
    assert len(out) == 4 and "school_ft" in out and "cul_de_sac_name" in out
    by_id = out.set_index("property_id")
    assert by_id.loc["3", "school_ft"] == pytest.approx(0.01 * 110574 * 3.28084, rel=0.01)
    assert pd.isna(by_id.loc["4", "school_ft"])  # no coordinates
    assert str(out["school_ft"].dtype) == "Int64"

    tight = add_nearby(df, ["school"], radius="100 ft", client=overpass)
    assert tight["school_ft"].isna().all()


def test_map_popup_shows_distances(fake_api, overpass):
    fake_api(homes=HOMES[:1])
    df = scrape_property("Lehigh Acres, FL", near={"cul_de_sac": "300 ft"})
    page = to_osm_map(df)
    assert "Cul-de-sac" in page
    assert "100 ft (Oak Ct)" in page


# ---------------------------------------------------------------------------
# Overpass client
# ---------------------------------------------------------------------------

class Resp:
    def __init__(self, status, body=None, text="", headers=None):
        self.status_code, self._body, self.text, self.headers = status, body, text, headers or {}

    def json(self):
        if self._body is None:
            raise ValueError
        return self._body


class Session:
    def __init__(self, script):
        self.script, self.calls = script, []

    def post(self, url, data, headers, timeout):
        self.calls.append((url, data, headers))
        return self.script.pop(0)


def make_overpass(script, tmp_path, **kw):
    sleeps = []
    session = Session(script)
    client = OverpassClient("https://overpass.example/api/interpreter", cache_dir=tmp_path, session=session,
                            sleep=sleeps.append, queries_per_second=None, **kw)
    return client, session, sleeps


def test_overpass_caches_and_identifies_generically(tmp_path):
    client, session, _ = make_overpass([Resp(200, {"elements": [1]})], tmp_path)
    assert client.query("[out:json];node(1);out;") == {"elements": [1]}
    assert client.query("[out:json];node(1);out;") == {"elements": [1]}  # second time from disk
    assert len(session.calls) == 1
    agent = session.calls[0][2]["User-Agent"]
    assert agent == DEFAULT_USER_AGENT and "ancgate" not in agent and "@" not in agent
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_overpass_retries_busy_server(tmp_path):
    script = [Resp(429, headers={"Retry-After": "30"}), Resp(504),
              Resp(200, {"remark": "runtime error: Query timed out", "elements": []}), Resp(200, {"elements": []})]
    client, session, sleeps = make_overpass(script, tmp_path)
    assert client.query("q") == {"elements": []}
    assert len(session.calls) == 4 and sleeps[0] >= 30


def test_overpass_errors(tmp_path):
    client, _, _ = make_overpass([Resp(400, text="parse error: line 1")], tmp_path)
    with pytest.raises(ValueError, match="rejected the query"):
        client.query("bad")
    client, _, _ = make_overpass([Resp(503)] * 3, tmp_path, max_attempts=3)
    with pytest.raises(OverpassError, match="after 3 attempts"):
        client.query("q2")


def test_overpass_cache_can_be_disabled(monkeypatch):
    monkeypatch.setenv("HOMEHARVEST_CACHE", "off")
    assert OverpassClient().cache_dir is None
    monkeypatch.setenv("HOMEHARVEST_OVERPASS_URL", "https://my-overpass.example/api/interpreter")
    assert OverpassClient().url == "https://my-overpass.example/api/interpreter"
