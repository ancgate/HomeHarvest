"""
homeharvest.mortgage
~~~~~~~~~~~~~~~~~~~~

Monthly payment and cash-to-close simulation, using the same arithmetic as Realtor.com's
"RealCost" card (reverse-engineered from its numbers; Realtor.com doesn't publish the formula):

* principal & interest - standard fixed-rate amortization:
  ``P&I = loan * r / (1 - (1 + r) ** -n)`` with ``r`` = annual rate / 12 and ``n`` = months;
* property tax and home insurance - home price x annual rate / 12;
* HOA - the listing's monthly fee;
* mortgage insurance (PMI) - only when the down payment is under 20%. **Assumption**: annual
  percent of the loan (default 0.5%); Realtor.com's own PMI rate is unknown;
* each part is rounded to the dollar and the total is the sum of the rounded parts;
* due at close = down payment + closing costs (default 4% of the price).

Checked against a Realtor.com card: $495,000, 20% down, 30-year fixed at 6.160%, tax 1.549%,
insurance 1.411% -> P&I $2,415, tax $639, insurance $582, total $3,636, due at close $118,800.

    >>> from homeharvest.mortgage import estimate_payment, simulate
    >>> estimate_payment(495_000, interest_rate_pct=6.16, property_tax_pct=1.549, home_insurance_pct=1.411)
    >>> simulate(495_000, interest_rate_pct=[5.5, 6.16, 7], down_payment_pct=[5, 10, 20],
    ...          property_tax_pct=1.549, home_insurance_pct=1.411)
"""

from __future__ import annotations

import itertools
from dataclasses import asdict, dataclass
from typing import Any

import pandas as pd

__all__ = ["PaymentEstimate", "principal_and_interest", "estimate_payment", "simulate",
           "amortization_schedule", "estimate_from_listing", "add_payment_estimates"]

#: Default annual PMI as a percent of the loan. An assumption (typical conventional PMI is ~0.3-1.5%).
DEFAULT_MORTGAGE_INSURANCE_PCT = 0.5
#: PMI is charged while the loan is above this share of the home's value (lenders drop it at 78%).
PMI_UNTIL_LTV = 0.78


@dataclass
class PaymentEstimate:
    home_price: float
    down_payment: float
    down_payment_pct: float
    loan_amount: float
    interest_rate_pct: float
    loan_term_years: int
    principal_and_interest: int
    property_tax: int
    home_insurance: int
    hoa: int
    mortgage_insurance: int
    total_monthly: int
    closing_cost: int
    due_at_close: int
    total_interest: int

    def as_dict(self) -> dict:
        return asdict(self)

    def __str__(self) -> str:
        lines = [
            f"${self.total_monthly:,}/month  (home ${self.home_price:,.0f}, {self.down_payment_pct:g}% down, "
            f"{self.loan_term_years}-year at {self.interest_rate_pct:g}%)",
            f"  Principal & interest  ${self.principal_and_interest:,}",
            f"  Property tax          ${self.property_tax:,}",
            f"  Home insurance        ${self.home_insurance:,}",
            f"  HOA fees              ${self.hoa:,}",
            f"  Mortgage insurance    ${self.mortgage_insurance:,}",
            f"Due at close ${self.due_at_close:,} (down payment ${self.down_payment:,.0f} + closing ${self.closing_cost:,})",
        ]
        return "\n".join(lines)


def principal_and_interest(loan_amount: float, interest_rate_pct: float, loan_term_years: int = 30) -> float:
    """Monthly principal & interest of a fixed-rate loan (unrounded)."""
    months = int(loan_term_years * 12)
    if months <= 0:
        raise ValueError("loan_term_years must be positive")
    if loan_amount <= 0:
        return 0.0
    rate = interest_rate_pct / 100 / 12
    if rate == 0:
        return loan_amount / months
    return loan_amount * rate / (1 - (1 + rate) ** -months)


def _round(value: float) -> int:
    """Round half up to the dollar (Python's round() would send 0.5 to the even number)."""
    return int(value + 0.5) if value >= 0 else -int(-value + 0.5)


def estimate_payment(home_price: float, *, interest_rate_pct: float, down_payment_pct: float = 20.0,
                     down_payment: float | None = None, loan_term_years: int = 30,
                     property_tax_pct: float | None = None, property_tax_annual: float | None = None,
                     home_insurance_pct: float | None = None, home_insurance_annual: float | None = None,
                     hoa_monthly: float = 0.0, mortgage_insurance_pct: float = DEFAULT_MORTGAGE_INSURANCE_PCT,
                     closing_cost_pct: float = 4.0) -> PaymentEstimate:
    """Monthly payment breakdown and cash due at close for one scenario.

    :param interest_rate_pct: Annual rate in percent, e.g. 6.16.
    :param down_payment_pct: Percent of the price (ignored if ``down_payment`` dollars are given).
    :param property_tax_pct / property_tax_annual: Annual tax as percent of price, or in dollars.
    :param home_insurance_pct / home_insurance_annual: Annual insurance as percent of price, or in dollars.
    :param mortgage_insurance_pct: Annual PMI as percent of the loan, charged below 20% down (an assumption).
    :param closing_cost_pct: Closing costs as percent of the price.
    """
    if home_price <= 0:
        raise ValueError("home_price must be positive")
    down = float(down_payment) if down_payment is not None else home_price * down_payment_pct / 100
    if not 0 <= down <= home_price:
        raise ValueError("down payment must be between 0 and the home price")
    loan = home_price - down
    pi = principal_and_interest(loan, interest_rate_pct, loan_term_years)

    tax = property_tax_annual if property_tax_annual is not None else home_price * (property_tax_pct or 0) / 100
    insurance = (home_insurance_annual if home_insurance_annual is not None
                 else home_price * (home_insurance_pct or 0) / 100)
    pmi = loan * mortgage_insurance_pct / 100 / 12 if loan / home_price > 0.80 + 1e-9 else 0.0
    closing = home_price * closing_cost_pct / 100

    parts = [_round(pi), _round(tax / 12), _round(insurance / 12), _round(hoa_monthly), _round(pmi)]
    return PaymentEstimate(
        home_price=float(home_price), down_payment=down, down_payment_pct=round(down / home_price * 100, 4),
        loan_amount=loan, interest_rate_pct=float(interest_rate_pct), loan_term_years=int(loan_term_years),
        principal_and_interest=parts[0], property_tax=parts[1], home_insurance=parts[2], hoa=parts[3],
        mortgage_insurance=parts[4], total_monthly=sum(parts), closing_cost=_round(closing),
        due_at_close=_round(down + closing),
        total_interest=_round(pi * loan_term_years * 12 - loan),
    )


def simulate(home_price: float | list, **scenarios) -> pd.DataFrame:
    """One row per combination of the scenario values. Any argument of estimate_payment may be a list.

        >>> simulate([450_000, 495_000], interest_rate_pct=[5.5, 6.16, 7.0], down_payment_pct=[5, 10, 20],
        ...          property_tax_pct=1.549, home_insurance_pct=1.411)
    """
    grid = {"home_price": home_price, **scenarios}
    keys = list(grid)
    values = [v if isinstance(v, (list, tuple, range)) else [v] for v in grid.values()]
    rows = [estimate_payment(**dict(zip(keys, combo))).as_dict() for combo in itertools.product(*values)]
    return pd.DataFrame(rows)


def amortization_schedule(home_price: float, *, interest_rate_pct: float, down_payment_pct: float = 20.0,
                          down_payment: float | None = None, loan_term_years: int = 30,
                          mortgage_insurance_pct: float = DEFAULT_MORTGAGE_INSURANCE_PCT,
                          extra_principal_monthly: float = 0.0) -> pd.DataFrame:
    """Month-by-month principal, interest, PMI and balance.

    PMI (below 20% down) stops once the balance reaches 78% of the home price. ``extra_principal_monthly``
    simulates paying extra each month (the loan then ends early).
    """
    down = float(down_payment) if down_payment is not None else home_price * down_payment_pct / 100
    balance = home_price - down
    payment = principal_and_interest(balance, interest_rate_pct, loan_term_years)
    rate = interest_rate_pct / 100 / 12
    pmi_monthly = balance * mortgage_insurance_pct / 100 / 12 if balance / home_price > 0.80 + 1e-9 else 0.0
    rows, total_interest = [], 0.0
    for month in range(1, int(loan_term_years * 12) + 1):
        if balance <= 0.005:
            break
        interest = balance * rate
        principal = min(balance, payment - interest + extra_principal_monthly)
        pmi = pmi_monthly if balance / home_price > PMI_UNTIL_LTV else 0.0
        balance -= principal
        total_interest += interest
        rows.append({"month": month, "year": (month - 1) // 12 + 1, "payment": round(principal + interest, 2),
                     "principal": round(principal, 2), "interest": round(interest, 2), "mortgage_insurance": round(pmi, 2),
                     "balance": round(max(balance, 0.0), 2), "total_interest": round(total_interest, 2)})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------

def _get(listing: Any, key: str):
    if isinstance(listing, dict):
        return listing.get(key)
    if isinstance(listing, pd.Series):
        return listing.get(key)
    if key in ("property_tax_rate", "insurance_rate", "mortgage_rate", "property_tax_monthly",
               "home_insurance_monthly") and getattr(listing, "insurance", None) is not None:
        return getattr(listing.insurance, key, None)
    return getattr(listing, key, None)


def _missing(value) -> bool:
    try:
        return value is None or bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _as_pct(value) -> float | None:
    """Realtor.com rates may come as a fraction (0.0155) or a percent (1.55): return percent."""
    if _missing(value):
        return None
    value = float(value)
    return value * 100 if value < 0.2 else value


def estimate_from_listing(listing: Any, **overrides) -> PaymentEstimate:
    """estimate_payment() for one scraped listing (DataFrame row, dict or Property model).

    Uses list_price, and when the listing was scraped with include_insurance=True its own property tax
    and insurance rates, HOA fee and mortgage rate. Anything can be overridden, e.g.
    ``estimate_from_listing(row, down_payment_pct=10, interest_rate_pct=6.5)``.
    """
    price = overrides.pop("home_price", None) or _get(listing, "list_price")
    if _missing(price) or float(price) <= 0:
        raise ValueError("The listing has no list_price; pass home_price=...")
    price = float(price)
    params: dict = {}

    rate = _as_pct(_get(listing, "mortgage_rate"))
    if rate is not None:
        params["interest_rate_pct"] = rate
    tax = _as_pct(_get(listing, "property_tax_rate"))
    if tax is not None:
        params["property_tax_pct"] = tax
    elif not _missing(_get(listing, "property_tax_monthly")):
        params["property_tax_annual"] = float(_get(listing, "property_tax_monthly")) * 12
    insurance = _as_pct(_get(listing, "insurance_rate"))
    if insurance is not None:
        params["home_insurance_pct"] = insurance
    elif not _missing(_get(listing, "home_insurance_monthly")):
        params["home_insurance_annual"] = float(_get(listing, "home_insurance_monthly")) * 12
    hoa = _get(listing, "hoa_fee")
    if not _missing(hoa):
        params["hoa_monthly"] = float(hoa)

    params.update(overrides)
    if "interest_rate_pct" not in params:
        raise ValueError("No mortgage rate on the listing (scrape with include_insurance=True) - pass interest_rate_pct=...")
    if "property_tax_pct" in overrides:
        params.pop("property_tax_annual", None)
    if "home_insurance_pct" in overrides:
        params.pop("home_insurance_annual", None)
    return estimate_payment(price, **params)


def add_payment_estimates(df: pd.DataFrame, prefix: str = "sim_", **overrides) -> pd.DataFrame:
    """Add simulated payment columns (sim_total_monthly, sim_due_at_close, ...) to scraped listings.

    Rows that can't be estimated (no price or rate) get empty values.
    """
    columns = ["total_monthly", "principal_and_interest", "property_tax", "home_insurance", "hoa",
               "mortgage_insurance", "down_payment", "loan_amount", "due_at_close", "interest_rate_pct"]
    values = {c: [] for c in columns}
    for _, row in df.iterrows():
        try:
            estimate = estimate_from_listing(row, **overrides).as_dict()
        except ValueError:
            estimate = {}
        for column in columns:
            values[column].append(estimate.get(column))
    out = df.copy()
    for column in columns:
        out[f"{prefix}{column}"] = values[column]
    return out
