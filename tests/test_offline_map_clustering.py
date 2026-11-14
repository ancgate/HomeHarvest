"""Offline tests for map clustering, keyword highlighting and generate_map (Phase 5)."""

import json
import xml.etree.ElementTree as ET

import pytest

from homeharvest import cluster_properties, generate_map, scrape_property, to_google_maps, to_kml, to_osm_map
from homeharvest.core.scrapers.realtor import RealtorScraper
from homeharvest.maps import property_points

from .fakes import FakeRealtorAPI, make_home

KML_NS = {"k": "http://www.opengis.net/kml/2.2"}

# Two neighborhoods ~7 miles apart. A cul-de-sac lot in the first one.
HOMES = [
    make_home("1", text="Lot on a quiet cul-de-sac", lat=26.6000, lon=-81.6000, postal_code="33971"),
    make_home("2", text="Nice lot", lat=26.6020, lon=-81.6010, postal_code="33971"),  # ~0.15 mi from 1
    make_home("3", text="Nice lot", lat=26.6100, lon=-81.6000, postal_code="33971"),  # ~0.7 mi from 1
    make_home("4", "sold", sold_days_ago=3, text="Nice home", lat=26.7000, lon=-81.6000, postal_code="33936"),
    make_home("5", text="Waterfront lot with seawall", lat=26.7010, lon=-81.6010, postal_code="33936"),
]


@pytest.fixture
def fake_api(monkeypatch):
    def install(**kwargs):
        api = FakeRealtorAPI(**kwargs)
        monkeypatch.setattr(RealtorScraper, "_graphql_post", lambda self, q, v, op: api(self, q, v, op))
        return api
    return install


@pytest.fixture
def df(fake_api):
    fake_api(homes=HOMES)
    return scrape_property("Lehigh Acres, FL")


def _payload(page: str) -> list[dict]:
    return json.loads(page.split("const POINTS = ", 1)[1].split(";\n", 1)[0])


def test_highlighting_marks_matches(df):
    points = property_points(df, features=["cul_de_sac"], nearby=None, keywords=None)
    assert [p["id"] for p in points if p["is_match"]] == ["1"]
    match = next(p for p in points if p["is_match"])
    assert match["color"] == "#d50000" and match["matched"] == ["cul_de_sac"]
    assert len(points) == 5  # non-matches are kept by default


def test_only_matching_and_near_matches(df):
    assert [p["id"] for p in property_points(df, nearby=["waterfront"], only_matching=True)] == ["5"]

    near = property_points(df, features=["cul_de_sac"], near_matches_miles=0.5)
    assert {p["id"]: p["near_match_miles"] for p in near} == {"1": None, "2": pytest.approx(0.15, abs=0.03)}
    assert near[1]["facts"][0][0] == "Near match"

    wider = property_points(df, features=["cul_de_sac"], near_matches_miles=1.0)
    assert {p["id"] for p in wider} == {"1", "2", "3"}
    assert property_points(df, keywords=["helipad"], near_matches_miles=1.0) == []


def test_existing_scrape_annotations_are_highlighted(fake_api):
    fake_api(homes=HOMES)
    matched = scrape_property("Lehigh Acres, FL", features=["cul_de_sac"])
    assert [p["is_match"] for p in property_points(matched)] == [True]


def test_osm_cluster_options(df):
    clustered = to_osm_map(df, features=["cul_de_sac"], cluster_radius=40, disable_clustering_at_zoom=15)
    assert "leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js" in clustered
    assert '"clusterRadius": 40' in clustered and '"disableAt": 15' in clustered
    assert [p["is_match"] for p in _payload(clustered)] == [True, False, False, False, False]

    plain = to_osm_map(df, cluster=False)
    assert "markercluster" not in plain and '"cluster": false' in plain

    with pytest.raises(TypeError, match="unexpected keyword"):
        to_osm_map(df, clusterr=True)


def test_google_cluster_script(df):
    page = to_google_maps(df, api_key="k", features=["cul_de_sac"])
    assert "@googlemaps/markerclusterer@2.5.3/dist/index.min.js" in page
    assert "markerClusterer.MarkerClusterer" in page
    assert "markerclusterer" not in to_google_maps(df, api_key="k", cluster=False)


def _folders(kml: str) -> list[str]:
    root = ET.fromstring(kml.split("\n", 1)[1])
    return [f.find("k:name", KML_NS).text for f in root.findall(".//k:Folder", KML_NS)]


def test_kml_cluster_by_variants(df):
    assert _folders(to_kml(df, features=["cul_de_sac"])) == ["★ Matches (1)", "For Sale (3)", "Sold (1)"]
    assert _folders(to_kml(df, features=["cul_de_sac"], cluster_by="match")) == ["★ Matches (1)", "Other properties (4)"]
    assert _folders(to_kml(df, cluster_by="zip_code")) == ["33971 (3)", "33936 (2)"]
    assert _folders(to_kml(df, features=["cul_de_sac"], cluster_by="proximity", cluster_radius_miles=1)) == [
        "Cluster 1 · 3 properties · Lehigh Acres · 1 ★", "Cluster 2 · 2 properties · Lehigh Acres"]
    assert _folders(to_kml(df, cluster_by=None)) == []
    root = ET.fromstring(to_kml(df, features=["cul_de_sac"]).split("\n", 1)[1])
    first = root.find(".//k:Placemark", KML_NS)
    assert first.find("k:name", KML_NS).text.startswith("★ ")
    assert first.find("k:styleUrl", KML_NS).text == "#match"
    with pytest.raises(ValueError, match="cluster_by"):
        to_kml(df, cluster_by="planet")


def test_cluster_properties(df):
    out = cluster_properties(df, radius_miles=1)
    assert out["cluster_id"].tolist() == [1, 1, 1, 2, 2]
    assert out["cluster_size"].tolist() == [3, 3, 3, 2, 2]
    assert out.loc[0, "cluster_center_lat"] == pytest.approx((26.6 + 26.602 + 26.61) / 3)
    assert cluster_properties(df, radius_miles=0.01)["cluster_id"].nunique() == 5
    with pytest.raises(ValueError):
        cluster_properties(df, radius_miles=0)


def test_generate_map_scrapes_and_highlights(fake_api, tmp_path):
    api = fake_api(homes=HOMES)
    df = generate_map("Lehigh Acres, FL", tmp_path / "map.html", listing_type="for_sale",
                      features=["cul_de_sac"], near_matches_miles=0.5, cluster_radius=30)
    assert len(df) == 4  # every for_sale listing is returned (the sold one is not); the map shows matches + nearby
    page = (tmp_path / "map.html").read_text()
    assert [p["is_match"] for p in _payload(page)] == [True, False]
    assert "<title>Lehigh Acres, FL (4 properties)</title>" in page
    assert "status: for_sale" in api.queries("GetHomeSearch")[0]


def test_generate_map_only_matching_filters_during_scrape(fake_api, tmp_path):
    fake_api(homes=HOMES)
    df = generate_map("Lehigh Acres, FL", tmp_path / "map.kml", nearby=["waterfront"], only_matching=True,
                      cluster_by="match")
    assert list(df["property_id"]) == ["5"]
    assert _folders((tmp_path / "map.kml").read_text().replace('<?xml version="1.0" encoding="UTF-8"?>\n', "x\n")) == [
        "★ Matches (1)"]
