"""Offline tests for automatic splitting past Realtor.com's 10,000-result cap.

The cap is patched down to 10 so a handful of fixture homes exercises the same code paths.
"""

import re
import warnings

import pytest

from homeharvest import scrape_property
from homeharvest.core.scrapers.realtor import RealtorScraper

from .fakes import FakeRealtorAPI, make_home

CAP = 10


@pytest.fixture
def fake_api(monkeypatch):
    monkeypatch.setattr(RealtorScraper, "API_RESULT_CAP", CAP)

    def install(homes):
        api = FakeRealtorAPI(homes=homes)
        api.cap = CAP
        monkeypatch.setattr(RealtorScraper, "_graphql_post", lambda self, q, v, op: api(self, q, v, op))
        return api
    return install


def for_sale(n, price=lambda i: 20_000 * (i + 1)):
    return [make_home(str(i), list_price=price(i)) for i in range(n)]


def _price_bands(api):
    """The list_price filter of every search that had one ("list_price" alone is also a result field)."""
    matches = (re.search(r"list_price: \{([^}]*)\}", q) for q in api.queries("GetHomeSearch"))
    return [m.group(1).strip() for m in matches if m]


def test_limit_none_collects_past_the_cap_by_price_band(fake_api):
    api = fake_api(for_sale(25))  # $20k ... $500k
    df = scrape_property("Lehigh Acres, FL", listing_type="for_sale", limit=None)
    assert len(df) == 25 and df["property_id"].is_unique
    bands = _price_bands(api)
    assert "min: 0.0 max: 49999.99" in bands and "min: 5000000.0" in bands
    assert api.schema_errors == []


def test_dense_band_is_halved_until_it_fits(fake_api):
    api = fake_api(for_sale(15, price=lambda i: 60_000 + 1_000 * i))  # all in the 50k-100k band
    df = scrape_property("Lehigh Acres, FL", listing_type="for_sale", limit=None)
    assert len(df) == 15
    bands = _price_bands(api)
    assert "min: 50000.0 max: 99999.99" in bands
    # halves are contiguous: no gap and no overlap between them
    assert "min: 50000.0 max: 74999.99" in bands and "min: 75000.0 max: 99999.99" in bands


def test_user_price_filters_bound_the_bands(fake_api):
    api = fake_api(for_sale(25))
    df = scrape_property("Lehigh Acres, FL", listing_type="for_sale", limit=None, price_min=100_000, price_max=400_000)
    assert sorted(df["list_price"].astype(int)) == list(range(100_000, 400_001, 20_000))
    assert all("min: 100000" in b or float(re.search(r"min: ([\d.]+)", b).group(1)) >= 100_000
               for b in _price_bands(api))


def test_limit_above_cap_stops_early(fake_api):
    fake_api(for_sale(25))
    assert len(scrape_property("Lehigh Acres, FL", listing_type="for_sale", limit=15)) == 15


def test_no_split_when_the_limit_fits(fake_api):
    api = fake_api(for_sale(25))
    df = scrape_property("Lehigh Acres, FL", listing_type="for_sale", limit=5)
    assert len(df) == 5 and len(api.queries("GetHomeSearch")) == 1


def test_sold_searches_split_by_sold_date_newest_first(fake_api):
    homes = [make_home(str(i), "sold", sold_days_ago=15 * i, list_days_ago=15 * i + 30) for i in range(25)]
    api = fake_api(homes)
    df = scrape_property("Lehigh Acres, FL", listing_type="sold", past_days=400, limit=None)
    assert len(df) == 25
    queries = api.queries("GetHomeSearch")
    assert 'sold_date: { min: "$today-400D" }' in queries[0]  # the first, unsplit search
    bands = [m.groups() for q in queries if (m := re.search(r'sold_date: \{ min: "([^"]+)", max: "([^"]+)" \}', q))]
    assert len(bands) > 3 and all(low <= high for low, high in bands)
    assert df["last_sold_date"].iloc[0] > df["last_sold_date"].iloc[-1]  # newest band first
    assert api.schema_errors == []


def test_all_listing_types_split_by_type_then_band(fake_api):
    homes = for_sale(12) + [make_home(f"s{i}", "sold", sold_days_ago=10 * i) for i in range(12)] \
        + [make_home(f"r{i}", "for_rent", list_price=1000 + 100 * i) for i in range(3)]
    fake_api(homes)
    df = scrape_property("Lehigh Acres, FL", limit=None)
    assert len(df) == 27
    assert set(df["status"]) == {"FOR_SALE", "SOLD", "FOR_RENT"}


def test_unsplittable_search_warns_and_caps(fake_api):
    fake_api([make_home(str(i), "off_market") for i in range(25)])
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        df = scrape_property("Lehigh Acres, FL", listing_type="off_market", limit=None)
    assert len(df) == CAP
    assert any("can't be split further" in str(w.message) for w in caught)


def test_identical_prices_past_the_cap_warn(fake_api):
    fake_api(for_sale(25, price=lambda i: 75_000))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        df = scrape_property("Lehigh Acres, FL", listing_type="for_sale", limit=None)
    assert len(df) == CAP
    assert any("can't be split" in str(w.message) for w in caught)


def test_limit_and_offset_validation():
    with pytest.raises(ValueError, match="at least 1"):
        scrape_property("Lehigh Acres, FL", limit=0)
    with pytest.raises(ValueError, match="offset"):
        scrape_property("Lehigh Acres, FL", limit=None, offset=200)
    with pytest.raises(ValueError, match="offset"):
        scrape_property("Lehigh Acres, FL", limit=9_900, offset=400)
