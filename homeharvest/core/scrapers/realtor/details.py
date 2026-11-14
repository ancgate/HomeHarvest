"""
Parsing and merging of opt-in property detail enrichment.

The scraper fetches extra ``home(property_id: ...)`` data only when the user asks for it
(``include_property_history``, ``include_environmental_risk``, ``include_insurance``,
``include_market_stats``). This module turns that raw GraphQL data into the pydantic models
in :mod:`homeharvest.core.scrapers.models` and merges it into search results.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..models import (
    EnvironmentalRisk,
    FloodRisk,
    HousingMarketStats,
    InsuranceInfo,
    InsuranceQuote,
    MarketStats,
    Property,
    PropertyHistoryEvent,
    RiskFactor,
)
from .processors import apply_buyers


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.strptime(str(value)[:10], "%Y-%m-%d")
        except ValueError:
            return None


# ---------------------------------------------------------------------------
# Property history (+ price changes)
# ---------------------------------------------------------------------------

_LISTED_EVENTS = ("listed", "relisted", "listing")


def parse_property_history(history: list[dict] | None) -> list[PropertyHistoryEvent] | None:
    if not history:
        return None
    events = []
    for event in history:
        if not isinstance(event, dict):
            continue
        events.append(
            PropertyHistoryEvent(
                date=_parse_datetime(event.get("date")),
                event_name=event.get("event_name"),
                price=_to_int(event.get("price")),
                price_change=_to_int(event.get("price_change")),
                price_sqft=_to_float(event.get("price_sqft")),
                source_name=event.get("source_name"),
                source_listing_id=event.get("source_listing_id"),
                status=event.get("status"),
            )
        )
    # Most recent first (Realtor.com's own ordering, made explicit). Undated events sort last.
    events.sort(key=lambda e: e.date.replace(tzinfo=None) if e.date else datetime.min, reverse=True)
    return events or None


def derive_original_list_price(events: list[PropertyHistoryEvent] | None) -> int | None:
    """Price of the most recent "Listed" event, i.e. the price the current listing started at.

    History is most-recent-first; walk it until we hit a listing event. A Sold event before
    reaching one means the property is not currently listed from that sale onward.
    """
    if not events:
        return None
    for event in events:
        name = (event.event_name or "").lower()
        if any(name.startswith(prefix) for prefix in _LISTED_EVENTS) and event.price:
            return event.price
        if name.startswith("sold"):
            return None
    return None


# ---------------------------------------------------------------------------
# Environmental risk
# ---------------------------------------------------------------------------

def _risk(data: dict | None, prefix: str, trend_key: str | None = None) -> RiskFactor | None:
    if not data:
        return None
    risk = RiskFactor(
        score=_to_int(data.get(f"{prefix}_factor_score")),
        severity=data.get(f"{prefix}_factor_severity"),
        trend=data.get(trend_key) if trend_key else None,
        report_url=data.get("firststreet_url"),
    )
    return risk if any(v is not None for v in risk.model_dump().values()) else None


def parse_environmental_risk(local: dict | None) -> EnvironmentalRisk | None:
    if not local:
        return None

    flood_data = local.get("flood") or {}
    flood = None
    if flood_data:
        flood = FloodRisk(
            score=_to_int(flood_data.get("flood_factor_score")),
            severity=flood_data.get("flood_factor_severity"),
            trend=flood_data.get("flood_trend"),
            report_url=flood_data.get("firststreet_url"),
            fema_zone=flood_data.get("fema_zone") or None,
            insurance_requirement=flood_data.get("insurance_requirement"),
            environmental_risk=_to_int(flood_data.get("environmental_risk")),
        )
        if not any(v is not None for v in flood.model_dump().values()):
            flood = None

    noise = local.get("noise") or {}
    risk = EnvironmentalRisk(
        flood=flood,
        fire=_risk(local.get("wildfire"), "fire", "fire_trend"),
        heat=_risk(local.get("heat"), "heat", "heat_trend"),
        wind=_risk(local.get("wind"), "wind", "wind_trend"),
        air=_risk(local.get("air"), "air", "air_trend"),
        noise_score=_to_float(noise.get("score")),
        noise_categories=noise.get("noise_categories") or None,
    )
    if not any(v is not None for v in risk.model_dump().values()):
        return None
    return risk


# ---------------------------------------------------------------------------
# Insurance (issue #121)
# ---------------------------------------------------------------------------

def _quotes(quotes: list[dict] | None) -> list[InsuranceQuote] | None:
    if not quotes:
        return None
    parsed = []
    for quote in quotes:
        if not isinstance(quote, dict):
            continue
        parsed.append(
            InsuranceQuote(
                provider_name=quote.get("provider_name"),
                provider_url=quote.get("provider_url"),
                price=_to_float(quote.get("price")),
                min_price=_to_float(quote.get("min_price")),
                max_price=_to_float(quote.get("max_price")),
                home_coverage=_to_float(quote.get("home_coverage")),
                contents_coverage=_to_float(quote.get("contents_coverage")),
                expires=_parse_datetime(quote.get("expires")),
            )
        )
    return parsed or None


def parse_insurance(detail: dict) -> InsuranceInfo | None:
    mortgage = detail.get("mortgage") or {}
    estimate = mortgage.get("estimate") or {}
    local = detail.get("local") or {}
    flood = local.get("flood") or {}
    wildfire = local.get("wildfire") or {}

    expenses = {}
    for item in estimate.get("monthly_payment_details") or []:
        if isinstance(item, dict) and item.get("type"):
            expenses[str(item["type"]).lower()] = _to_int(item.get("amount"))

    info = InsuranceInfo(
        home_insurance_monthly=expenses.get("home_insurance"),
        insurance_rate=_to_float(mortgage.get("insurance_rate")),
        property_tax_rate=_to_float(mortgage.get("property_tax_rate")),
        property_tax_monthly=expenses.get("property_tax"),
        hoa_monthly=expenses.get("hoa_fees"),
        mortgage_insurance_monthly=expenses.get("mortgage_insurance"),
        principal_and_interest_monthly=expenses.get("principal_and_interest"),
        estimated_monthly_payment=_to_int(estimate.get("monthly_payment")),
        flood_insurance_requirement=flood.get("insurance_requirement"),
        flood_insurance_quotes=_quotes(flood.get("insurance_rates")),
        fire_insurance_quotes=_quotes(wildfire.get("insurance_rates")),
    )
    if not any(v is not None for v in info.model_dump().values()):
        return None
    return info


# ---------------------------------------------------------------------------
# Market stats (issue #137)
# ---------------------------------------------------------------------------

def _housing_market(geo: dict | None) -> HousingMarketStats | None:
    if not geo:
        return None
    market = (geo.get("geo_statistics") or {}).get("housing_market") or {}
    if not market:
        return None
    stats = HousingMarketStats(
        median_rent_price=_to_float(market.get("median_rent_price")),
        median_listing_price=_to_float(market.get("median_listing_price")),
        median_sold_price=_to_float(market.get("median_sold_price")),
        median_price_per_sqft=_to_float(market.get("median_price_per_sqft")),
        median_days_on_market=_to_float(market.get("median_days_on_market")),
    )
    return stats if any(v is not None for v in stats.model_dump().values()) else None


def parse_market_stats(detail: dict) -> MarketStats | None:
    rent = detail.get("rent_amount_model") or {}
    location = detail.get("location") or {}

    neighborhood_name, neighborhood_stats = None, None
    for neighborhood in location.get("neighborhoods") or []:
        stats = _housing_market(neighborhood)
        if stats:
            neighborhood_name, neighborhood_stats = neighborhood.get("name"), stats
            break

    postal = location.get("postal_code")
    zip_stats = _housing_market(postal) if isinstance(postal, dict) else None

    market = MarketStats(
        estimated_rent=_to_int(rent.get("value")),
        estimated_rent_low=_to_int(rent.get("value_low")),
        estimated_rent_high=_to_int(rent.get("value_high")),
        neighborhood_name=neighborhood_name,
        neighborhood=neighborhood_stats,
        zip_code=zip_stats,
    )
    if not any(v is not None for v in market.model_dump().values()):
        return None
    return market


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def apply_details_to_property(prop: Property, detail: dict, include: set[str]) -> Property:
    """Attach parsed detail data to a Property model (in place) and return it."""
    if not detail:
        return prop

    if "property_history" in include:
        events = parse_property_history(detail.get("property_history"))
        prop.property_history = events
        prop.original_list_price = derive_original_list_price(events)
        prop.last_price_change_amount = _to_int(detail.get("last_price_change_amount"))
        prop.last_price_change_date = _parse_datetime(detail.get("last_price_change_date"))
        prop.advertisers = apply_buyers(prop.advertisers, detail.get("buyers"))

    if "environmental_risk" in include:
        prop.environmental_risk = parse_environmental_risk(detail.get("local"))

    if "insurance" in include:
        prop.insurance = parse_insurance(detail)

    if "market_stats" in include:
        prop.market_stats = parse_market_stats(detail)

    return prop


def apply_details_to_raw(home: dict, detail: dict) -> dict:
    """Merge raw detail data into a raw GraphQL home dict (in place) and return it."""
    if not detail:
        return home
    for key, value in detail.items():
        if key == "property_id":
            continue
        if key in ("location", "local") and isinstance(value, dict) and isinstance(home.get(key), dict):
            for sub_key, sub_value in value.items():
                home[key][sub_key] = sub_value
        else:
            home[key] = value
    return home
