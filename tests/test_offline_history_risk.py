"""Offline tests for opt-in property history and environmental risk (Phase 6)."""

import pytest

from homeharvest import scrape_property, to_osm_map
from homeharvest.core.scrapers.models import PropertyHistoryEvent
from homeharvest.core.scrapers.realtor import RealtorScraper
from homeharvest.core.scrapers.realtor.details import derive_original_list_price, parse_property_history

from .fakes import FakeRealtorAPI, make_home

HISTORY_DETAIL = {
    "property_id": "1",
    "last_price_change_amount": -10000.0,
    "last_price_change_date": "2025-10-20T00:00:00Z",
    "property_history": [
        {"date": "2025-10-20", "event_name": "Price Changed", "price": 290000, "price_change": -10000,
         "price_sqft": 161.1, "source_name": "Florida Gulf Coast MLS", "source_listing_id": "225001", "status": None},
        {"date": "2025-09-01", "event_name": "Listed", "price": 300000, "price_change": 0,
         "price_sqft": 166.7, "source_name": "Florida Gulf Coast MLS", "source_listing_id": "225001", "status": None},
        {"date": "2019-05-15", "event_name": "Sold", "price": 180000, "price_change": 0,
         "price_sqft": 100, "source_name": "Public Record", "source_listing_id": None, "status": None},
    ],
    "buyers": [
        {"type": "buyer", "name": "Bea Buyer-Agent", "email": "bea@x.com", "fulfillment_id": "77",
         "phones": [{"number": "555-0199", "type": "Office", "primary": True, "ext": None}],
         "office": {"name": "Gulf Buyers Realty", "fulfillment_id": "88"}, "broker": {"name": "Gulf Brokers"}},
    ],
}

RISK_DETAIL = {
    "property_id": "1",
    "local": {
        "flood": {"flood_factor_score": 6, "flood_factor_severity": "Major", "flood_trend": "increasing",
                  "fema_zone": ["AE"], "insurance_requirement": "Flood insurance required",
                  "environmental_risk": 6, "firststreet_url": "https://firststreet.org/property/1"},
        "wildfire": {"fire_factor_score": 1, "fire_factor_severity": "Minimal", "fire_trend": None,
                     "firststreet_url": "https://firststreet.org/property/1"},
        "heat": {"heat_factor_score": 8, "heat_factor_severity": "Severe", "heat_trend": None, "firststreet_url": None},
        "wind": {"wind_factor_score": 9, "wind_factor_severity": "Extreme", "wind_trend": None, "firststreet_url": None},
        "air": {"air_factor_score": 2, "air_factor_severity": "Minor", "air_trend": None, "firststreet_url": None},
        "noise": {"score": 71.0, "noise_categories": [{"type": "traffic", "text": "Moderate"}]},
    },
}


@pytest.fixture
def fake_api(monkeypatch):
    def install(**kwargs):
        api = FakeRealtorAPI(**kwargs)
        monkeypatch.setattr(RealtorScraper, "_graphql_post", lambda self, q, v, op: api(self, q, v, op))
        return api
    return install


# ---------------------------------------------------------------------------
# Property history
# ---------------------------------------------------------------------------

def test_include_property_history(fake_api):
    api = fake_api(homes=[make_home("1", list_price=290000)], details={"1": HISTORY_DETAIL})
    df = scrape_property("Lehigh Acres, FL", listing_type="for_sale", include_property_history=True)

    row = df.iloc[0]
    assert row["original_list_price"] == 300000
    assert row["last_price_change_amount"] == -10000
    assert row["last_price_change_date"] == "2025-10-20 00:00:00"
    history = row["property_history"]
    assert [e["event_name"] for e in history] == ["Price Changed", "Listed", "Sold"]  # newest first
    assert history[2]["price"] == 180000 and history[0]["source_name"] == "Florida Gulf Coast MLS"
    # buyer side comes from home.buyers
    assert row["buyer_agent_name"] == "Bea Buyer-Agent"
    assert row["buyer_office_name"] == "Gulf Buyers Realty"
    assert row["buyer_broker_name"] == "Gulf Brokers"

    (query,) = api.queries("GetPropertyDetails")
    assert "property_history" in query and "buyers" in query and "local" not in query
    assert api.schema_errors == []


@pytest.mark.parametrize("events,expected", [
    ([("2025-10-01", "Price Changed", 290000), ("2025-09-01", "Listed", 300000)], 300000),
    ([("2025-10-01", "Relisted", 310000), ("2025-08-01", "Listing Removed", 305000),
      ("2025-03-01", "Listed", 320000)], 310000),
    ([("2025-10-01", "Sold", 280000), ("2025-06-01", "Listed", 300000)], None),  # not on the market anymore
    ([], None),
])
def test_original_list_price(events, expected):
    parsed = parse_property_history([{"date": d, "event_name": n, "price": p} for d, n, p in events])
    assert derive_original_list_price(parsed) == expected


def test_history_is_sorted_newest_first_with_undated_last():
    parsed = parse_property_history([
        {"date": None, "event_name": "Unknown"},
        {"date": "2020-01-01", "event_name": "Sold", "price": 1},
        {"date": "2024-01-01T10:00:00Z", "event_name": "Listed", "price": 2},
    ])
    assert [e.event_name for e in parsed] == ["Listed", "Sold", "Unknown"]
    assert isinstance(parsed[0], PropertyHistoryEvent)


# ---------------------------------------------------------------------------
# Environmental risk
# ---------------------------------------------------------------------------

def test_include_environmental_risk(fake_api):
    api = fake_api(homes=[make_home("1")], details={"1": RISK_DETAIL})
    df = scrape_property("Lehigh Acres, FL", listing_type="for_sale", include_environmental_risk=True)

    row = df.iloc[0]
    assert (row["flood_factor_score"], row["flood_factor_severity"]) == (6, "Major")
    assert row["flood_fema_zone"] == "AE"
    assert row["flood_insurance_requirement"] == "Flood insurance required"
    assert (row["fire_factor_score"], row["fire_factor_severity"]) == (1, "Minimal")
    assert row["heat_factor_severity"] == "Severe" and row["wind_factor_score"] == 9
    assert row["air_factor_severity"] == "Minor" and row["noise_score"] == 71.0
    assert row["environmental_risk_url"] == "https://firststreet.org/property/1"

    (query,) = api.queries("GetPropertyDetails")
    assert "flood_factor_score" in query and "property_history" not in query
    assert api.schema_errors == []


def test_environmental_risk_pydantic_and_raw(fake_api):
    fake_api(homes=[make_home("1")], details={"1": RISK_DETAIL})
    (prop,) = scrape_property("Lehigh Acres, FL", listing_type="for_sale", include_environmental_risk=True,
                              return_type="pydantic")
    assert prop.environmental_risk.flood.fema_zone == ["AE"]
    assert prop.environmental_risk.wind.severity == "Extreme"
    assert prop.environmental_risk.noise_categories == [{"type": "traffic", "text": "Moderate"}]

    (raw,) = scrape_property("Lehigh Acres, FL", listing_type="for_sale", include_environmental_risk=True,
                             return_type="raw")
    assert raw["local"]["flood"]["flood_factor_score"] == 6


def test_flags_off_by_default(fake_api):
    api = fake_api(homes=[make_home("1")], details={"1": RISK_DETAIL})
    df = scrape_property("Lehigh Acres, FL", listing_type="for_sale")
    assert "flood_factor_score" not in df.columns and "property_history" not in df.columns
    assert api.queries("GetPropertyDetails") == []


def test_all_flags_share_one_batched_query(fake_api):
    merged = {**HISTORY_DETAIL, **RISK_DETAIL}
    api = fake_api(homes=[make_home("1")], details={"1": merged})
    df = scrape_property("Lehigh Acres, FL", listing_type="for_sale", include_property_history=True,
                         include_environmental_risk=True, include_insurance=True, include_market_stats=True)
    assert len(api.queries("GetPropertyDetails")) == 1
    assert df.iloc[0]["flood_factor_severity"] == "Major" and df.iloc[0]["original_list_price"] == 300000
    assert api.schema_errors == []


def test_missing_risk_data_leaves_columns_empty(fake_api):
    fake_api(homes=[make_home("1")], details={"1": {"property_id": "1", "local": None}})
    df = scrape_property("Lehigh Acres, FL", listing_type="for_sale", include_environmental_risk=True)
    assert df.iloc[0][["flood_factor_score", "fire_factor_score"]].isna().all()


def test_risk_appears_in_map_popups(fake_api):
    fake_api(homes=[make_home("1")], details={"1": RISK_DETAIL})
    df = scrape_property("Lehigh Acres, FL", listing_type="for_sale", include_environmental_risk=True)
    page = to_osm_map(df)
    assert "Flood risk" in page and "Major (6/10)" in page and "FEMA zone" in page
