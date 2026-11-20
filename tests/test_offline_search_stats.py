"""Offline tests for the search completeness report used by tracking (phase 1)."""

import warnings

import pytest

from homeharvest import scrape_property
from homeharvest._context import SEARCH_STATS
from homeharvest.core.scrapers.realtor import RealtorScraper

from .fakes import FakeRealtorAPI, make_home

CAP = 10


@pytest.fixture
def fake_api(monkeypatch):
    monkeypatch.setattr(RealtorScraper, "API_RESULT_CAP", CAP)

    def install(homes, location=None):
        api = FakeRealtorAPI(homes=homes, location=location)
        api.cap = CAP
        monkeypatch.setattr(RealtorScraper, "_graphql_post", lambda self, q, v, op: api(self, q, v, op))
        return api
    return install


def run(**kwargs):
    stats = {}
    token = SEARCH_STATS.set(stats)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result = scrape_property("Lehigh Acres, FL", **kwargs)
    finally:
        SEARCH_STATS.reset(token)
    return result, stats


def for_sale(n):
    return [make_home(str(i), list_price=20_000 * (i + 1)) for i in range(n)]


def test_complete_search(fake_api):
    fake_api(for_sale(8))
    df, stats = run(listing_type="for_sale")
    assert len(df) == 8
    assert stats == {"location_found": True, "reported_total": 8, "capped": False, "truncated_by_limit": False}


def test_split_search_past_cap_is_complete(fake_api):
    fake_api(for_sale(25))
    df, stats = run(listing_type="for_sale", limit=None)
    assert len(df) == 25 and stats["capped"] is False and stats["truncated_by_limit"] is False
    assert stats["reported_total"] == 25


def test_unsplittable_search_is_capped(fake_api):
    fake_api([make_home(str(i), "off_market") for i in range(25)])
    df, stats = run(listing_type="off_market", limit=None)
    assert len(df) == CAP and stats["capped"] is True


def test_limit_truncation(fake_api):
    fake_api(for_sale(25))
    _, small = run(listing_type="for_sale", limit=5)
    assert small["truncated_by_limit"] is True and small["capped"] is False
    _, banded = run(listing_type="for_sale", limit=15)      # stops part-way through the price bands
    assert banded["truncated_by_limit"] is True
    fake_api(for_sale(8))
    _, enough = run(listing_type="for_sale", limit=8)       # limit equals the number of matches
    assert enough["truncated_by_limit"] is False


def test_location_not_found(fake_api, monkeypatch):
    fake_api(for_sale(3))
    monkeypatch.setattr(RealtorScraper, "handle_location", lambda self: None)
    df, stats = run(listing_type="for_sale")
    assert df.empty and stats["location_found"] is False


def test_scrape_property_unchanged_without_sink(fake_api):
    fake_api(for_sale(3))
    assert SEARCH_STATS.get() is None
    assert len(scrape_property("Lehigh Acres, FL", listing_type="for_sale")) == 3
