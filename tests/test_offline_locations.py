"""Offline tests for homeharvest.locations."""

import warnings

import pandas as pd
import pytest

from homeharvest import (
    aggregate_locations,
    get_cities,
    get_counties,
    get_location_hierarchy,
    get_state,
    get_states,
    get_zip_codes,
    scrape_property,
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


def echo_location(term):
    """Resolve any search term to itself (so tests can see the scope that was requested)."""
    return {"text": term, "geo": {"area_type": "city", "centroid": {"lat": 1, "lon": 1}}}


# Realtor.com-style aggregation per scope
AGGREGATIONS = {
    "Florida": {"county": [{"key": "Lee", "count": 900}, {"key": "Collier", "count": 300}]},
    "Lee County, FL": {"city": [{"key": "Lehigh Acres", "count": 500}, {"key": "Fort Myers", "count": 400}]},
    "Collier County, FL": {"city": [{"key": "Naples", "count": 300}]},
    "Lehigh Acres, FL": {"postal_code": [{"key": "33971", "count": 300}, {"key": "33936", "count": 200}]},
    "Fort Myers, FL": {"postal_code": [{"key": "33901", "count": 400}]},
    "Naples, FL": {"postal_code": [{"key": "34102", "count": 300}]},
}


def by_scope(location, query):
    return AGGREGATIONS.get(location, {})


def test_states():
    states = get_states()
    assert len(states) == 51  # 50 states + DC
    assert {"code": "MO", "name": "Missouri", "fips": "29"} in states
    assert len(get_states(include_territories=True)) == 56
    assert list(get_states(return_type="pandas").columns) == ["code", "name", "fips"]
    assert get_state("texas")["code"] == "TX" and get_state("tx")["name"] == "Texas" and get_state("48")["code"] == "TX"
    with pytest.raises(ValueError):
        get_state("Atlantis")


def test_get_counties(fake_api):
    api = fake_api(location=echo_location, aggregation=by_scope)
    assert get_counties("FL") == ["Collier", "Lee"]
    df = get_counties("Florida", with_counts=True)
    assert df.to_dict("records") == [{"county": "Collier", "listing_count": 300}, {"county": "Lee", "listing_count": 900}]

    query = api.queries("GetLocationAggregates")[0]
    assert "aggregate: { field: [county]" in query
    assert "status: [for_sale, for_rent, sold, off_market]" in query
    assert api.schema_errors == []


def test_get_cities_and_zip_codes_scopes(fake_api):
    fake_api(location=echo_location, aggregation=by_scope)
    assert get_cities("FL", county="Lee") == ["Fort Myers", "Lehigh Acres"]
    assert get_zip_codes(state="FL", city="Lehigh Acres") == ["33936", "33971"]
    with pytest.raises(ValueError, match="state="):
        get_cities(None, county="Lee")


def test_listing_type_scopes_statuses(fake_api):
    api = fake_api(location=echo_location, aggregation=by_scope)
    get_counties("FL", listing_type=["for_sale", "pending"])
    (query,) = api.queries("GetLocationAggregates")
    assert "status: [for_sale]" in query


def test_county_names_for_louisiana_use_parish(fake_api):
    api = fake_api(location=echo_location, aggregation=lambda loc, q: {"city": [{"key": "Metairie", "count": 1}]})
    get_cities("LA", county="Jefferson")
    assert api.calls[0][2]["searchInput"]["search_term"] == "Jefferson Parish, LA"


def test_fallback_to_listings_when_no_aggregation(fake_api):
    fake_api(location=echo_location, aggregation=None, homes=[
        make_home("1", county="Lee"), make_home("2", county="Lee"), make_home("3", county="Collier")])
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        df = get_counties("FL", with_counts=True)
    assert df.to_dict("records") == [{"county": "Collier", "listing_count": 1}, {"county": "Lee", "listing_count": 2}]
    assert any("falling back" in str(w.message) for w in caught)


def test_location_hierarchy_for_county(fake_api):
    fake_api(location=echo_location, aggregation=by_scope)
    df = get_location_hierarchy("FL", county="Lee")
    assert df.to_dict("records") == [
        {"state": "FL", "state_name": "Florida", "county": "Lee", "city": "Lehigh Acres", "zip_code": "33971", "listing_count": 300},
        {"state": "FL", "state_name": "Florida", "county": "Lee", "city": "Lehigh Acres", "zip_code": "33936", "listing_count": 200},
        {"state": "FL", "state_name": "Florida", "county": "Lee", "city": "Fort Myers", "zip_code": "33901", "listing_count": 400},
    ]


def test_location_hierarchy_for_state_and_guard(fake_api):
    fake_api(location=echo_location, aggregation=by_scope)
    df = get_location_hierarchy("FL")
    assert set(df["county"]) == {"Lee", "Collier"}
    assert set(df["zip_code"]) == {"33971", "33936", "33901", "34102"}
    no_zip = get_location_hierarchy("FL", include_zip_codes=False)
    assert no_zip["zip_code"].isna().all() and set(no_zip["city"]) == {"Lehigh Acres", "Fort Myers", "Naples"}
    with pytest.raises(ValueError, match="max_requests"):
        get_location_hierarchy("FL", max_requests=2)


def test_aggregate_locations_dataframe_and_tree(fake_api):
    fake_api(homes=[
        make_home("1", list_price=100000, city="Lehigh Acres", postal_code="33971"),
        make_home("2", list_price=300000, city="Lehigh Acres", postal_code="33971"),
        make_home("3", "sold", sold_days_ago=3, list_price=200000, city="Lehigh Acres", postal_code="33936"),
        make_home("4", list_price=500000, city="Fort Myers", postal_code="33901"),
    ])
    df = scrape_property("Lee County, FL")

    agg = aggregate_locations(df)
    top = agg.iloc[0]
    assert (top["city"], top["zip_code"], top["listing_count"]) == ("Lehigh Acres", "33971", 2)
    assert top["median_list_price"] == 200000
    assert top["for_sale_count"] == 2 and top["sold_count"] == 0

    by_city = aggregate_locations(df, by=["city"])
    assert dict(zip(by_city["city"], by_city["listing_count"])) == {"Lehigh Acres": 3, "Fort Myers": 1}

    tree = aggregate_locations(df, as_tree=True)
    assert tree == {"FL": {"Lee": {"Fort Myers": {"33901": 1}, "Lehigh Acres": {"33936": 1, "33971": 2}}}}

    models = scrape_property("Lee County, FL", return_type="pydantic")
    assert aggregate_locations(models, by=["zip_code"])["listing_count"].sum() == 4

    with pytest.raises(ValueError):
        aggregate_locations(df, by=["planet"])
    assert aggregate_locations(pd.DataFrame()).empty
