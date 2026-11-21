"""Offline tests for the RealCost fields and the payment simulation."""

import pytest

from homeharvest import scrape_property
from homeharvest.core.scrapers.realtor import RealtorScraper
from homeharvest.mortgage import (add_payment_estimates, amortization_schedule, estimate_from_listing,
                                  estimate_payment, principal_and_interest, simulate)

from .fakes import FakeRealtorAPI, make_home

#: The Realtor.com RealCost card this was reverse-engineered from
CARD = dict(home_price=495_000, interest_rate_pct=6.16, property_tax_pct=1.549, home_insurance_pct=1.411)

REALCOST_DETAIL = {
    "property_id": "1",
    "mortgage": {
        "insurance_rate": 0.01411, "property_tax_rate": 0.01549,
        "due_at_close": 118800, "closing_cost": 19800, "closing_cost_rate": "4%",
        "average_rates": [{"rate": 6.16, "loan_type": {"loan_id": "thirty_year_fix", "term": 30,
                                                       "display_name": "30-year fixed", "is_fixed": True,
                                                       "is_va_loan": False}}],
        "estimate": {
            "monthly_payment": 3636, "total_payment": 869400, "loan_amount": 396000, "down_payment": 99000,
            "average_rate": {"rate": 6.16, "loan_type": {"loan_id": "thirty_year_fix", "term": 30,
                                                         "display_name": "30-year fixed", "is_fixed": True}},
            "monthly_payment_details": [
                {"type": "principal_and_interest", "amount": 2415, "display_name": "Principal & interest"},
                {"type": "property_tax", "amount": 639, "display_name": "Property tax"},
                {"type": "home_insurance", "amount": 582, "display_name": "Home insurance"},
                {"type": "hoa_fees", "amount": 0, "display_name": "HOA fees"},
                {"type": "mortgage_insurance", "amount": 0, "display_name": "Mortgage insurance"},
            ],
        },
    },
}


@pytest.fixture
def listing(monkeypatch):
    api = FakeRealtorAPI(homes=[make_home("1", list_price=495_000)], details={"1": REALCOST_DETAIL})
    monkeypatch.setattr(RealtorScraper, "_graphql_post", lambda self, q, v, op: api(self, q, v, op))
    df = scrape_property("Lehigh Acres, FL", listing_type="for_sale", include_insurance=True)
    assert api.schema_errors == []
    return df


def test_every_realcost_field_is_a_column(listing):
    row = listing.iloc[0]
    assert (row["estimated_monthly_payment"], row["principal_and_interest_monthly"], row["property_tax_monthly"],
            row["home_insurance_monthly"], row["hoa_monthly"], row["mortgage_insurance_monthly"]) == (
        3636, 2415, 639, 582, 0, 0)
    assert (row["mortgage_rate"], row["loan_type"], row["loan_term_years"]) == (6.16, "30-year fixed", 30)
    assert (row["loan_amount"], row["down_payment"], row["closing_cost"], row["due_at_close"]) == (
        396000, 99000, 19800, 118800)
    assert row["closing_cost_rate"] == "4%"


def test_formula_reproduces_the_card():
    e = estimate_payment(**CARD)
    assert (e.principal_and_interest, e.property_tax, e.home_insurance, e.hoa, e.mortgage_insurance) == (
        2415, 639, 582, 0, 0)
    assert e.total_monthly == 3636 and e.loan_amount == 396_000
    assert (e.down_payment, e.closing_cost, e.due_at_close) == (99_000, 19_800, 118_800)
    assert principal_and_interest(396_000, 6.16) == pytest.approx(2415.11, abs=0.01)


def test_estimate_from_scraped_listing_matches_realtor(listing):
    e = estimate_from_listing(listing.iloc[0])          # rates come as fractions; normalized to percent
    assert e.total_monthly == listing.iloc[0]["estimated_monthly_payment"] == 3636
    assert estimate_from_listing(listing.iloc[0], down_payment_pct=10).mortgage_insurance > 0
    out = add_payment_estimates(listing, down_payment_pct=10, interest_rate_pct=7)
    assert out.iloc[0]["sim_interest_rate_pct"] == 7 and out.iloc[0]["sim_down_payment"] == 49_500


def test_pmi_only_below_20_percent_and_dollar_inputs():
    assert estimate_payment(**{**CARD, "down_payment_pct": 20}).mortgage_insurance == 0
    low = estimate_payment(**{**CARD, "down_payment_pct": 10})
    assert low.mortgage_insurance == round(445_500 * 0.005 / 12)
    assert estimate_payment(**{**CARD, "down_payment_pct": 10, "mortgage_insurance_pct": 0}).mortgage_insurance == 0
    dollars = estimate_payment(495_000, interest_rate_pct=6.16, down_payment=99_000, property_tax_annual=7_668,
                               home_insurance_annual=6_984, hoa_monthly=125)
    assert (dollars.property_tax, dollars.home_insurance, dollars.hoa, dollars.total_monthly) == (639, 582, 125, 3761)


def test_simulate_grid():
    df = simulate(495_000, interest_rate_pct=[5.5, 6.16, 7.0], down_payment_pct=[5, 10, 20],
                  property_tax_pct=1.549, home_insurance_pct=1.411)
    assert len(df) == 9
    assert df.query("interest_rate_pct == 6.16 and down_payment_pct == 20")["total_monthly"].item() == 3636
    assert df["total_monthly"].is_monotonic_increasing is False and df["principal_and_interest"].min() == 2248


def test_amortization_schedule():
    s = amortization_schedule(495_000, interest_rate_pct=6.16, down_payment_pct=10)
    assert len(s) == 360 and s.iloc[-1]["balance"] == 0
    assert s.iloc[0]["payment"] == pytest.approx(principal_and_interest(445_500, 6.16), abs=0.01)
    # PMI is charged while the balance entering the month is above 78% of the price
    first_free = int(s[s["mortgage_insurance"] == 0]["month"].min())
    entering = s.set_index("month")["balance"]
    assert entering[first_free - 1] <= 495_000 * 0.78 < entering[first_free - 2]
    assert (s[s["month"] < first_free]["mortgage_insurance"] > 0).all()
    faster = amortization_schedule(495_000, interest_rate_pct=6.16, extra_principal_monthly=500)
    assert len(faster) < 360 and faster.iloc[-1]["total_interest"] < s.iloc[-1]["total_interest"]


def test_zero_rate_and_validation():
    assert principal_and_interest(360_000, 0, 30) == 1000
    with pytest.raises(ValueError):
        estimate_payment(0, interest_rate_pct=6)
    with pytest.raises(ValueError):
        estimate_payment(100_000, interest_rate_pct=6, down_payment=200_000)
    with pytest.raises(ValueError, match="interest_rate_pct"):
        estimate_from_listing({"list_price": 300_000})
