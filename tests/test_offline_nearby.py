"""Offline tests for the land / "nearby" categories (Phase 4)."""

import pytest

from homeharvest import list_features, match_property, scrape_property
from homeharvest.core.scrapers.realtor import RealtorScraper

from .fakes import FakeRealtorAPI, make_home

NEARBY = [
    "farms_and_ranches", "hunting", "timberland", "undeveloped", "recreational", "commercial", "horse",
    "homesite", "house", "lakefront", "riverfront", "oceanfront", "waterfront",
]


@pytest.fixture
def fake_api(monkeypatch):
    def install(**kwargs):
        api = FakeRealtorAPI(**kwargs)
        monkeypatch.setattr(RealtorScraper, "_graphql_post", lambda self, q, v, op: api(self, q, v, op))
        return api
    return install


def test_catalog_has_every_requested_category():
    assert [f["key"] for f in list_features("nearby")] == NEARBY


@pytest.mark.parametrize("category,text", [
    ("farms_and_ranches", "40 acres of pasture, perfect for cattle"),
    ("hunting", "Great deer and turkey hunting"),
    ("timberland", "Heavily wooded with merchantable timber"),
    ("undeveloped", "Raw land, unimproved"),
    ("recreational", "Weekend getaway with ATV trails and fishing"),
    ("commercial", "Zoned commercial, mixed use allowed"),
    ("horse", "Equestrian property with a riding arena"),
    ("homesite", "Ready to build homesite"),
    ("house", "Includes a home"),
    ("lakefront", "Lakefront lot with dock"),
    ("riverfront", "200 ft of river frontage"),
    ("oceanfront", "Beachfront parcel"),
    ("waterfront", "Waterfront with seawall"),
])
def test_each_category_matches_its_phrasing(category, text):
    assert match_property({"text": text}, [category]).features == [category]


@pytest.mark.parametrize("category,text", [
    ("farms_and_ranches", "Single-story ranch style home with barn doors and a farm sink"),
    ("hunting", "Minutes from Hunters Creek and Deer Park"),
    ("timberland", "Wood floors throughout, close to Forest Hills"),
    ("commercial", "Commercial grade appliances, close to commercial areas"),
    ("horse", "Shower stalls, 300 horsepower boat"),
    ("recreational", "Recreation room and RV parking"),
])
def test_look_alikes_do_not_match(category, text):
    assert match_property({"text": text}, [category]).features == []


def test_structural_predicates():
    vacant = {"text": "", "style": "LAND"}
    home = {"text": "", "style": "SINGLE_FAMILY", "beds": 3, "sqft": 1400}
    assert match_property(vacant, ["undeveloped", "house"]).features == ["undeveloped"]
    result = match_property(home, ["undeveloped", "house"])
    assert result.features == ["house"] and result.evidence["house"] == "has a home (3 beds, 1,400 sqft)"
    assert match_property({"text": "", "style": "FARM"}, ["farms_and_ranches"]).evidence == {
        "farms_and_ranches": "property type FARM"}


def test_waterfront_includes_lake_river_ocean_and_canal():
    for text in ("on the lake", "river frontage", "gulf front", "canal front with gulf access"):
        assert match_property({"text": text}, ["waterfront"]).features == ["waterfront"], text


def test_scrape_property_nearby_filter(fake_api):
    fake_api(homes=[
        make_home("1", style="land", beds=None, sqft=None, text="Lakefront lot on a cul-de-sac"),
        make_home("2", style="land", beds=None, sqft=None, text="River frontage, wooded"),
        make_home("3", style="land", beds=None, sqft=None, text="Cul-de-sac lot near shopping"),
        make_home("4", text="Lakefront home on a cul-de-sac"),
    ])

    water = scrape_property("Lehigh Acres, FL", nearby=["lakefront", "riverfront"])
    assert list(water["property_id"]) == ["1", "2", "4"]

    both = scrape_property("Lehigh Acres, FL", property_type=["land"], features=["cul_de_sac"],
                           nearby=["lakefront", "riverfront"])
    assert list(both["property_id"]) == ["1", "4"]  # fake API ignores property_type
    assert both.iloc[0]["matched_features"] == "cul_de_sac, lakefront"

    vacant_on_water = scrape_property("Lehigh Acres, FL", nearby=["undeveloped", "waterfront"], match_mode="all")
    assert list(vacant_on_water["property_id"]) == ["1", "2"]


def test_nearby_rejects_unknown_category():
    with pytest.raises(ValueError, match="Unknown nearby value 'lake'.*lakefront"):
        scrape_property("Lehigh Acres, FL", nearby=["lake"])
