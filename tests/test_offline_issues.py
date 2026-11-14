"""
Offline tests for fixes to upstream GitHub issues (ZacharyHampton/HomeHarvest).

These use a fake GraphQL transport (tests/fakes.py) so they run without network access, and
every query the scraper builds is validated against the bundled introspection schema.
"""

import warnings

import pandas as pd
import pytest

from homeharvest import scrape_property
from homeharvest.core.scrapers.realtor import RealtorScraper

from .fakes import ADDRESS_LOCATION, FakeRealtorAPI, make_home
from .schema_validator import validate_query
from homeharvest.core.scrapers.realtor.queries import (
    GENERAL_RESULTS_QUERY,
    HOMES_DATA,
    DETAIL_INSURANCE_FIELDS,
    DETAIL_MARKET_STATS_FIELDS,
    build_property_details_query,
)


@pytest.fixture
def fake_api(monkeypatch):
    def install(**kwargs):
        api = FakeRealtorAPI(**kwargs)
        monkeypatch.setattr(RealtorScraper, "_graphql_post", lambda self, q, v, op: api(self, q, v, op))
        return api
    return install


def _mixed_homes():
    return [
        make_home("1", "for_sale", list_days_ago=0),
        make_home("2", "for_rent", list_days_ago=0),
        make_home("3", "sold", sold_days_ago=0, list_days_ago=40),
        make_home("4", "for_sale", list_days_ago=90, is_pending=True, pending_days_ago=0),
    ]


# ---------------------------------------------------------------------------
# Issue #143 - past_days ignored without a listing_type / with a list of types
# ---------------------------------------------------------------------------

def test_past_days_without_listing_type_filters_each_type(fake_api):
    api = fake_api(homes=_mixed_homes())

    result = scrape_property(location="Lehigh Acres, FL", past_days=1)

    searches = api.queries("GetHomeSearch")
    assert len(searches) == 4, "one search per listing type (for_sale, for_rent, sold, pending)"
    joined = "\n".join(searches)
    assert 'sold_date: { min: "$today-1D" }' in joined
    assert joined.count('list_date: { min: "$today-1D" }') == 2  # for_sale + for_rent
    assert "or_filters: { contingent: true, pending: true }" in joined  # pending search
    assert set(result["property_id"]) == {"1", "2", "3", "4"}
    assert result["property_id"].is_unique
    assert api.schema_errors == []


def test_past_days_with_listing_type_list(fake_api):
    api = fake_api(homes=_mixed_homes())

    result = scrape_property(location="Lehigh Acres, FL", listing_type=["for_sale", "sold"], past_days=7)

    searches = api.queries("GetHomeSearch")
    assert len(searches) == 2
    assert any('status: sold' in q and 'sold_date: { min: "$today-7D" }' in q for q in searches)
    assert any('status: for_sale' in q and 'list_date: { min: "$today-7D" }' in q for q in searches)
    assert set(result["status"]) <= {"FOR_SALE", "SOLD", "PENDING"}
    assert api.schema_errors == []


def test_single_element_listing_type_list_keeps_date_filter(fake_api):
    api = fake_api(homes=_mixed_homes())

    scrape_property(location="Lehigh Acres, FL", listing_type=["sold"], past_days=30)

    (search,) = api.queries("GetHomeSearch")
    assert 'sold_date: { min: "$today-30D" }' in search


def test_listing_type_none_without_dates_is_unchanged(fake_api):
    api = fake_api(homes=_mixed_homes())

    scrape_property(location="Lehigh Acres, FL")

    (search,) = api.queries("GetHomeSearch")
    assert "status: [for_sale, for_rent, sold, off_market]" in search


def test_split_search_resolves_location_once(fake_api):
    api = fake_api(homes=_mixed_homes())
    scrape_property(location="Lehigh Acres, FL", past_days=3)
    assert len(api.queries("Search_suggestions")) == 1


def test_split_search_respects_limit(fake_api):
    fake_api(homes=_mixed_homes())
    result = scrape_property(location="Lehigh Acres, FL", past_days=3, limit=2)
    assert len(result) == 2


# ---------------------------------------------------------------------------
# Issue #134 - buyer's agent for sold listings
# ---------------------------------------------------------------------------

def test_buyer_agent_columns_from_advertisers(fake_api):
    advertisers = [
        {"type": "seller", "name": "Sally Seller", "fulfillment_id": "11", "email": "s@x.com",
         "office": {"name": "Listing Realty", "fulfillment_id": "21"}, "broker": {"name": "List Brokers"}},
        {"type": "buyer", "name": "Bob Buyer", "fulfillment_id": "12", "email": "b@x.com",
         "phones": [{"number": "555-0100", "type": "Mobile", "primary": True, "ext": None}],
         "office": {"name": "Buyer Realty", "fulfillment_id": "22"}, "broker": {"name": "Buy Brokers"}},
    ]
    fake_api(homes=[make_home("9", "sold", sold_days_ago=5, advertisers=advertisers)])

    df = scrape_property(location="Lehigh Acres, FL", listing_type="sold")

    row = df.iloc[0]
    assert row["agent_name"] == "Sally Seller"
    assert row["buyer_agent_name"] == "Bob Buyer"
    assert row["buyer_agent_email"] == "b@x.com"
    assert row["buyer_broker_name"] == "Buy Brokers"
    assert row["buyer_office_name"] == "Buyer Realty"

    props = scrape_property(location="Lehigh Acres, FL", listing_type="sold", return_type="pydantic")
    assert props[0].advertisers.buyer_agent.name == "Bob Buyer"


# ---------------------------------------------------------------------------
# Issue #120 - expired listings (mls_status filter)
# ---------------------------------------------------------------------------

def test_mls_status_filter_matches_substrings_case_insensitively(fake_api):
    fake_api(homes=[
        make_home("1", "off_market", mls_status="Expired"),
        make_home("2", "off_market", mls_status="Withdrawn"),
        make_home("3", "off_market", mls_status="Off Market"),
        make_home("4", "off_market", mls_status=None),
        make_home("5", "off_market", mls_status="Expired Listing"),
    ])

    expired = scrape_property(location="Lehigh Acres, FL", listing_type="off_market", mls_status="expired")
    assert set(expired["property_id"]) == {"1", "5"}

    either = scrape_property(location="Lehigh Acres, FL", listing_type="off_market",
                             mls_status=["EXPIRED", "withdrawn"], return_type="raw")
    assert {h["property_id"] for h in either} == {"1", "2", "5"}


# ---------------------------------------------------------------------------
# Issues #121 (insurance) and #137 (rent / neighborhood medians)
# ---------------------------------------------------------------------------

INSURANCE_DETAIL = {
    "property_id": "1",
    "mortgage": {
        "insurance_rate": 0.0042,
        "property_tax_rate": 0.0119,
        "estimate": {
            "monthly_payment": 2350,
            "monthly_payment_details": [
                {"type": "principal_and_interest", "amount": 1700, "display_name": "Principal & Interest"},
                {"type": "property_tax", "amount": 248, "display_name": "Property Tax"},
                {"type": "home_insurance", "amount": 175, "display_name": "Home Insurance"},
                {"type": "hoa_fees", "amount": 0, "display_name": "HOA fees"},
            ],
        },
    },
    "local": {
        "flood": {
            "insurance_requirement": "Flood insurance is not required",
            "insurance_rates": [{"provider_name": "Neptune", "provider_url": "https://neptune.example",
                                 "price": 650.0, "min_price": None, "max_price": None,
                                 "home_coverage": 250000, "contents_coverage": 100000, "expires": None}],
        },
        "wildfire": {"insurance_rates": None},
    },
}

MARKET_DETAIL = {
    "property_id": "1",
    "rent_amount_model": {"value": 2100, "value_low": 1900.0, "value_high": 2300.0},
    "location": {
        "neighborhoods": [{"name": "Westwood", "geo_statistics": {"housing_market": {
            "median_rent_price": 1412, "median_listing_price": 249900, "median_sold_price": 240000,
            "median_price_per_sqft": 160, "median_days_on_market": 45}}}],
        "postal_code": {"postal_code": "33971", "geo_statistics": {"housing_market": {
            "median_rent_price": 1500, "median_listing_price": 260000, "median_sold_price": None,
            "median_price_per_sqft": None, "median_days_on_market": None}}},
    },
}


def _merge(*dicts):
    out = {}
    for d in dicts:
        for k, v in d.items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k] = {**out[k], **v}
            else:
                out[k] = v
    return out


def test_include_insurance_and_market_stats(fake_api):
    api = fake_api(homes=[make_home("1", "for_rent")], details={"1": _merge(INSURANCE_DETAIL, MARKET_DETAIL)})

    df = scrape_property(location="Lehigh Acres, FL", listing_type="for_rent",
                         include_insurance=True, include_market_stats=True)

    row = df.iloc[0]
    assert row["home_insurance_monthly"] == 175
    assert row["insurance_rate"] == pytest.approx(0.0042)
    assert row["estimated_monthly_payment"] == 2350
    assert row["property_tax_monthly"] == 248
    assert row["flood_insurance_requirement"] == "Flood insurance is not required"
    assert row["flood_insurance_quotes"][0]["provider_name"] == "Neptune"
    assert row["estimated_rent"] == 2100
    assert row["neighborhood_median_rent_price"] == 1412
    assert row["neighborhood_median_listing_price"] == 249900
    assert row["zip_median_rent_price"] == 1500

    (details_query,) = api.queries("GetPropertyDetails")
    assert "monthly_payment_details" in details_query and "median_rent_price" in details_query
    assert api.schema_errors == []


def test_optional_columns_absent_unless_requested(fake_api):
    api = fake_api(homes=[make_home("1")])
    df = scrape_property(location="Lehigh Acres, FL", listing_type="for_sale")
    assert "home_insurance_monthly" not in df.columns
    assert "estimated_rent" not in df.columns
    assert api.queries("GetPropertyDetails") == []


def test_include_flags_with_raw_return_merge_raw_data(fake_api):
    fake_api(homes=[make_home("1", "for_rent")], details={"1": MARKET_DETAIL})
    (home,) = scrape_property(location="Lehigh Acres, FL", listing_type="for_rent",
                              include_market_stats=True, return_type="raw")
    assert home["rent_amount_model"]["value"] == 2100
    # location is deep-merged: the address from the search is kept
    assert home["location"]["address"]["city"] == "Lehigh Acres"
    assert home["location"]["neighborhoods"][0]["geo_statistics"]["housing_market"]["median_rent_price"] == 1412


def test_details_are_batched(fake_api, monkeypatch):
    homes = [make_home(str(i)) for i in range(1, 61)]
    api = fake_api(homes=homes, details={str(i): {"property_id": str(i)} for i in range(1, 61)})
    scrape_property(location="Lehigh Acres, FL", listing_type="for_sale", include_insurance=True)
    batches = api.queries("GetPropertyDetails")
    assert len(batches) == 3  # 60 homes / 25 per batch


def test_detail_query_errors_warn_but_keep_results(fake_api):
    fake_api(homes=[make_home("1")], details_error={
        "data": None, "errors": [{"message": 'Cannot query field "insurance_rate" on type "Mortgage".'}]})

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        df = scrape_property(location="Lehigh Acres, FL", listing_type="for_sale", include_insurance=True)

    assert len(df) == 1
    assert pd.isna(df.iloc[0]["home_insurance_monthly"])
    assert any("extra property details" in str(w.message) for w in caught)


# ---------------------------------------------------------------------------
# Schema guard rails for queries the library sends
# ---------------------------------------------------------------------------

def test_static_queries_match_schema():
    assert validate_query("query GetHomeDetails($property_id: ID!) { home(property_id: $property_id) %s }" % HOMES_DATA) == []
    assert validate_query("query S($offset: Int!) { homeSearch: home_search(query: {status: for_sale} limit: 200 offset: $offset) %s }"
                          % GENERAL_RESULTS_QUERY) == []
    assert validate_query(build_property_details_query(["1"], [DETAIL_INSURANCE_FIELDS, DETAIL_MARKET_STATS_FIELDS])) == []


def test_single_address_search_is_enriched(fake_api):
    api = fake_api(homes=[make_home("1")], location=ADDRESS_LOCATION, details={"1": INSURANCE_DETAIL})
    df = scrape_property(location="100 Main St, Lehigh Acres, FL 33971", include_insurance=True)
    assert len(df) == 1
    assert df.iloc[0]["home_insurance_monthly"] == 175
    assert len(api.queries("GetHomeDetails")) == 1
    assert api.schema_errors == []
