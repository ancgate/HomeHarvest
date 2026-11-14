"""Offline tests for keyword/feature matching and map exports."""

import csv
import io
import json
import xml.etree.ElementTree as ET

import pandas as pd
import pytest

from homeharvest import (
    export_map,
    filter_properties,
    list_features,
    match_property,
    scrape_property,
    to_geojson,
    to_google_maps,
    to_google_my_maps_csv,
    to_kml,
    to_osm_map,
)
from homeharvest.core.scrapers.realtor import RealtorScraper

from .fakes import FakeRealtorAPI, make_home


@pytest.fixture
def fake_api(monkeypatch):
    def install(**kwargs):
        api = FakeRealtorAPI(**kwargs)
        monkeypatch.setattr(RealtorScraper, "_graphql_post", lambda self, q, v, op: api(self, q, v, op))
        return api
    return install


def _land(pid, text, **kw):
    kw.setdefault("style", "land")
    return make_home(pid, text=text, beds=None, sqft=None, **kw)


LAND = [
    _land("1", "Build your dream home on this quiet Cul-De-Sac lot. City water available."),
    _land("2", "Oversized lot close to shopping.", street_name="Oak", street_suffix="Ct"),
    _land("3", "Corner lot. No pool permitted by HOA.", tags=["corner_lot"]),
    _land("4", "Wooded acreage.", details=[{"category": "Lot Features", "text": ["Cul-De-Sac", "Wooded"]}]),
]


# ---------------------------------------------------------------------------
# Feature matching
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["on a cul-de-sac", "Cul De Sac location", "culdesac lot", "CUL-DE-SAC!"])
def test_cul_de_sac_spellings(text):
    assert match_property({"text": text}, ["cul_de_sac"]).features == ["cul_de_sac"]


def test_match_sources_and_evidence():
    by_tag = match_property({"text": "", "tags": "central_air, cul_de_sac"}, ["cul_de_sac"])
    assert by_tag.evidence["cul_de_sac"] == 'tag "cul_de_sac"'

    raw = LAND[3]
    by_details = match_property(raw, ["cul_de_sac"])
    assert by_details.evidence["cul_de_sac"].startswith("details:")

    by_street = match_property({"text": "Nice lot", "street": "123 Oak Ct W"}, ["cul_de_sac", "likely_cul_de_sac"])
    assert by_street.features == ["likely_cul_de_sac"]
    assert "CT suffix" in by_street.evidence["likely_cul_de_sac"]


def test_negation_is_ignored():
    assert match_property({"text": "There is no pool."}, ["pool"]).features == []
    assert match_property({"text": "Not near a golf course."}, ["golf_course"]).features == []
    assert match_property({"text": "No HOA, pool and spa."}, ["pool", "no_hoa"]).features == ["pool", "no_hoa"]


def test_keywords_are_spacing_and_punctuation_tolerant():
    result = match_property({"text": "Quiet Cul-De-Sac with a sea wall"}, keywords=["culdesac", "seawall", "cul de sac"])
    assert result.keywords == ["culdesac", "seawall", "cul de sac"]
    assert match_property({"text": "carpool lane"}, keywords=["pool"]).keywords == []


def test_unknown_feature_suggests_alternatives():
    with pytest.raises(ValueError, match="Did you mean: cul_de_sac"):
        match_property({"text": ""}, ["culdesack"])


def test_list_features_catalog():
    keys = {f["key"] for f in list_features()}
    assert {"cul_de_sac", "likely_cul_de_sac", "corner_lot", "pool"} <= keys
    assert all(f["group"] == "feature" for f in list_features("feature"))


def test_scrape_property_features_filter(fake_api):
    fake_api(homes=LAND)
    df = scrape_property("Lehigh Acres, FL", property_type=["land"], features=["cul_de_sac"])
    assert list(df["property_id"]) == ["1", "4"]
    assert list(df["matched_features"]) == ["cul_de_sac", "cul_de_sac"]
    assert df["feature_evidence"].str.contains("cul_de_sac").all()

    broad = scrape_property("Lehigh Acres, FL", features=["likely_cul_de_sac"])
    assert list(broad["property_id"]) == ["1", "2", "4"]


def test_match_mode_any_vs_all(fake_api):
    fake_api(homes=LAND)
    any_df = scrape_property("Lehigh Acres, FL", features=["cul_de_sac", "corner_lot"])
    all_df = scrape_property("Lehigh Acres, FL", features=["cul_de_sac", "city_water_sewer"], match_mode="all")
    assert set(any_df["property_id"]) == {"1", "3", "4"}
    assert list(all_df["property_id"]) == ["1"]


def test_features_and_keywords_groups_are_combined_with_and(fake_api):
    fake_api(homes=LAND)
    df = scrape_property("Lehigh Acres, FL", features=["cul_de_sac"], keywords=["wooded"])
    assert list(df["property_id"]) == ["4"]


def test_features_with_raw_and_pydantic_returns(fake_api):
    fake_api(homes=LAND)
    raw = scrape_property("Lehigh Acres, FL", features=["corner_lot"], return_type="raw")
    assert [h["property_id"] for h in raw] == ["3"] and raw[0]["matched_features"] == ["corner_lot"]
    models = scrape_property("Lehigh Acres, FL", features=["corner_lot"], return_type="pydantic")
    assert models[0].matched_features == ["corner_lot"]


def test_filter_properties_on_dataframe(fake_api):
    fake_api(homes=LAND)
    df = scrape_property("Lehigh Acres, FL")
    only = filter_properties(df, features=["cul_de_sac"])
    assert list(only["property_id"]) == ["1"]  # DataFrames have no feature details column
    annotated = filter_properties(df, keywords=["oversized"], only_matching=False)
    assert len(annotated) == len(df)
    assert annotated.set_index("property_id").loc["2", "matched_keywords"] == "oversized"


# ---------------------------------------------------------------------------
# Map exports
# ---------------------------------------------------------------------------

@pytest.fixture
def map_df(fake_api):
    fake_api(homes=[
        LAND[0],
        make_home("5", "sold", sold_days_ago=2, lat=26.7, lon=-81.7, text="<b>bold</b> & <script>x()</script>"),
        make_home("6", "for_rent", list_price=1900, lat=26.65, lon=-81.65),
        make_home("7", lat=None, lon=None),
    ])
    return scrape_property("Lehigh Acres, FL")


def test_geojson_export(map_df, tmp_path):
    collection = to_geojson(map_df, tmp_path / "map.geojson")
    assert collection["type"] == "FeatureCollection"
    assert len(collection["features"]) == 3  # home 7 has no coordinates
    feature = collection["features"][0]
    assert feature["geometry"]["coordinates"] == [-81.6, 26.6]
    assert feature["properties"]["realtor_url"].startswith("https://www.realtor.com/")
    assert json.loads((tmp_path / "map.geojson").read_text())["features"]


def test_kml_export(map_df, tmp_path):
    kml = to_kml(map_df, tmp_path / "map.kml")
    root = ET.fromstring((tmp_path / "map.kml").read_bytes())
    ns = {"k": "http://www.opengis.net/kml/2.2"}
    placemarks = root.findall(".//k:Placemark", ns)
    assert len(placemarks) == 3
    folders = [f.find("k:name", ns).text for f in root.findall(".//k:Folder", ns)]
    assert folders == ["For Sale (1)", "Sold (1)", "For Rent (1)"]
    description = placemarks[0].find("k:description", ns).text
    assert "View on Realtor.com" in description and "https://www.realtor.com/" in description
    assert "&lt;script&gt;" in kml and "<script>x()" not in kml


def test_osm_html_export(map_df, tmp_path):
    page = to_osm_map(map_df, tmp_path / "map.html", title="Lehigh <Acres>")
    assert "unpkg.com/leaflet@1.9.4/dist/leaflet.js" in page
    assert "tile.openstreetmap.org" in page
    assert "<title>Lehigh &lt;Acres&gt;</title>" in page
    assert "<script>x()" not in page  # listing text is escaped, never raw HTML
    payload = page.split("const POINTS = ", 1)[1].split(";\n", 1)[0]
    points = json.loads(payload)
    assert len(points) == 3 and all("View on Realtor.com" in p["popup"] for p in points)


def test_google_maps_html_requires_key(map_df, tmp_path):
    with pytest.raises(ValueError, match="API key"):
        to_google_maps(map_df, tmp_path / "g.html", api_key="")
    page = to_google_maps(map_df, tmp_path / "g.html", api_key="abc 123")
    assert "maps.googleapis.com/maps/api/js?key=abc+123&callback=initMap" in page


def test_my_maps_csv_export_and_split(map_df, tmp_path):
    (text,) = to_google_my_maps_csv(map_df, tmp_path / "mymaps.csv")
    rows = list(csv.DictReader(io.StringIO(text)))
    assert len(rows) == 3
    assert rows[0]["Realtor.com link"].startswith("https://www.realtor.com/")
    assert rows[0]["Latitude"] == "26.6"

    texts = to_google_my_maps_csv(map_df, tmp_path / "split.csv", max_rows_per_file=2)
    assert len(texts) == 2
    assert (tmp_path / "split_part1.csv").exists() and (tmp_path / "split_part2.csv").exists()


def test_export_map_dispatch(map_df, tmp_path):
    for name in ("m.html", "m.kml", "m.geojson", "m.csv"):
        assert export_map(map_df, tmp_path / name).endswith(name)
        assert (tmp_path / name).stat().st_size > 0
    with pytest.raises(ValueError, match="Unknown map format"):
        export_map(map_df, tmp_path / "m.txt")


def test_exports_accept_pydantic_and_raw(fake_api, tmp_path):
    fake_api(homes=[LAND[0], make_home("5", lat=26.7, lon=-81.7)])
    models = scrape_property("Lehigh Acres, FL", return_type="pydantic")
    raw = scrape_property("Lehigh Acres, FL", return_type="raw")
    assert len(to_geojson(models)["features"]) == 2
    assert len(to_geojson(raw)["features"]) == 2
