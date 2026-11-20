"""Offline tests for city/town/village place types (OpenStreetMap)."""

import pandas as pd
import pytest

from homeharvest import add_place_types, aggregate_locations, get_cities, scrape_property
from homeharvest import nearby as nearby_module
from homeharvest.core.scrapers.realtor import RealtorScraper

from .fakes import FakeRealtorAPI, make_home

ELEMENTS = [
    # Lehigh Acres: a census-designated place - a place node, but no municipal boundary
    {"type": "node", "id": 10, "lat": 26.61, "lon": -81.62,
     "tags": {"place": "town", "name": "Lehigh Acres", "population": "114,287"}},
    # Fort Myers: incorporated city
    {"type": "node", "id": 11, "lat": 26.64, "lon": -81.87, "tags": {"place": "city", "name": "Fort Myers",
                                                                       "population": "86395"}},
    {"type": "relation", "id": 21, "center": {"lat": 26.63, "lon": -81.85},
     "tags": {"boundary": "administrative", "admin_level": "8", "name": "Fort Myers", "border_type": "city"}},
    # Two "Springfield" places in one state: the one near the listings wins
    {"type": "node", "id": 12, "lat": 30.0, "lon": -85.0, "tags": {"place": "village", "name": "Springfield"}},
    {"type": "node", "id": 13, "lat": 27.0, "lon": -82.0, "tags": {"place": "hamlet", "name": "Springfield"}},
    # A municipal boundary named Springfield far away from both - must not mark them incorporated
    {"type": "relation", "id": 22, "center": {"lat": 25.0, "lon": -80.0},
     "tags": {"boundary": "administrative", "admin_level": "8", "name": "Springfield", "border_type": "town"}},
]


class FakeOverpass:
    def __init__(self):
        self.queries = []

    def query(self, ql):
        self.queries.append(ql)
        return {"elements": ELEMENTS}


@pytest.fixture
def overpass(monkeypatch):
    fake = FakeOverpass()
    monkeypatch.setattr(nearby_module, "get_overpass_client", lambda url=None: fake)
    return fake


def test_place_types_for_cities_towns_and_cdps(overpass):
    df = pd.DataFrame({
        "city": ["Lehigh Acres", "Fort Myers", "Springfield", "Atlantis"],
        "state": ["FL", "Florida", "FL", "FL"],
        "latitude": [26.6, 26.64, 27.01, 26.0],
        "longitude": [-81.6, -81.87, -82.01, -81.0],
    })
    out = add_place_types(df).set_index("city")

    assert out.loc["Lehigh Acres", "place_type"] == "town"
    assert out.loc["Lehigh Acres", "incorporated"] == False   # noqa: E712 - census-designated place
    assert pd.isna(out.loc["Lehigh Acres", "municipal_type"])
    assert out.loc["Lehigh Acres", "population"] == 114287

    assert out.loc["Fort Myers", "place_type"] == "city"
    assert out.loc["Fort Myers", "incorporated"] == True  # noqa: E712
    assert out.loc["Fort Myers", "municipal_type"] == "city"
    assert out.loc["Fort Myers", "place_osm_url"] == "https://www.openstreetmap.org/node/11"

    assert out.loc["Springfield", "place_type"] == "hamlet"     # the one near the listings
    assert out.loc["Springfield", "incorporated"] == False  # noqa: E712 - the far boundary isn't this place

    assert pd.isna(out.loc["Atlantis", "place_type"]) and pd.isna(out.loc["Atlantis", "incorporated"])
    assert str(out["incorporated"].dtype) == "boolean"
    assert len(overpass.queries) == 1                           # one query for the whole state


def test_query_text(overpass):
    add_place_types(pd.DataFrame({"city": ["St. Petersburg"], "state": ["FL"]}))
    (ql,) = overpass.queries
    assert 'area["ISO3166-2"="US-FL"]["admin_level"="4"]->.st;' in ql
    assert 'node(area.st)["place"]["name"="St. Petersburg"];' in ql
    assert 'node(area.st)["place"]["name"="Saint Petersburg"];' in ql
    assert 'relation(area.st)["boundary"="administrative"]["admin_level"="8"]["name"="St. Petersburg"];' in ql
    assert ql.rstrip().endswith(".towns out tags center;")


def test_works_on_scrape_results_and_aggregates(monkeypatch, overpass):
    api = FakeRealtorAPI(homes=[make_home("1", city="Lehigh Acres", lat=26.6, lon=-81.6),
                                make_home("2", city="Fort Myers", lat=26.64, lon=-81.87)])
    monkeypatch.setattr(RealtorScraper, "_graphql_post", lambda self, q, v, op: api(self, q, v, op))
    df = add_place_types(scrape_property("Lee County, FL"))
    assert dict(zip(df["city"], df["place_type"])) == {"Lehigh Acres": "town", "Fort Myers": "city"}

    by_city = add_place_types(aggregate_locations(df, by=["state", "city"]))
    assert set(by_city.columns) >= {"listing_count", "place_type", "incorporated"}


def test_get_cities_with_place_types(monkeypatch, overpass):
    api = FakeRealtorAPI(location=lambda term: {"text": term, "geo": {"area_type": "county"}},
                         aggregation={"city": [{"key": "Lehigh Acres", "count": 500}, {"key": "Fort Myers", "count": 400}]})
    monkeypatch.setattr(RealtorScraper, "_graphql_post", lambda self, q, v, op: api(self, q, v, op))
    df = get_cities("FL", county="Lee", place_type=True)
    assert list(df.columns[:3]) == ["state", "city", "listing_count"]
    assert df.set_index("city").loc["Lehigh Acres", "incorporated"] == False  # noqa: E712


def test_empty_or_missing_columns():
    out = add_place_types(pd.DataFrame({"zip_code": ["33971"]}))
    assert "place_type" in out.columns and out["place_type"].isna().all()
