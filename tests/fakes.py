"""
Offline test doubles for the Realtor.com GraphQL API.

``FakeRealtorAPI`` replaces ``RealtorScraper._graphql_post`` so tests can exercise the real
query-building and processing code without network access. Every query the scraper sends is
recorded and validated against the bundled introspection schema.
"""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from .schema_validator import validate_query


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_home(
    property_id: str,
    status: str = "for_sale",
    *,
    list_price: int = 250000,
    list_days_ago: int | None = 2,
    sold_days_ago: int | None = None,
    pending_days_ago: int | None = None,
    is_pending: bool = False,
    is_contingent: bool = False,
    text: str | None = "Lovely home.",
    tags: list[str] | None = None,
    details: list[dict] | None = None,
    style: str = "single_family",
    beds: int | None = 3,
    sqft: int | None = 1800,
    lot_sqft: int | None = 10000,
    street_number: str = "100",
    street_name: str = "Main",
    street_suffix: str = "St",
    city: str = "Lehigh Acres",
    state_code: str = "FL",
    postal_code: str = "33971",
    county: str = "Lee",
    lat: float | None = 26.6,
    lon: float | None = -81.6,
    mls_status: str | None = "Active",
    advertisers: list[dict] | None = None,
) -> dict:
    now = datetime.now(timezone.utc)
    line = f"{street_number} {street_name} {street_suffix}"
    return {
        "property_id": property_id,
        "listing_id": f"L{property_id}",
        "href": f"https://www.realtor.com/realestateandhomes-detail/{property_id}",
        "permalink": f"{street_number}-{street_name}-{street_suffix}_{city.replace(' ', '-')}_{state_code}_{postal_code}_M{property_id}",
        "status": status,
        "mls_status": mls_status,
        "list_price": list_price,
        "list_price_min": None,
        "list_price_max": None,
        "list_date": _iso(now - timedelta(days=list_days_ago)) if list_days_ago is not None else None,
        "last_sold_date": _iso(now - timedelta(days=sold_days_ago)) if sold_days_ago is not None else None,
        "last_sold_price": list_price if sold_days_ago is not None else None,
        "pending_date": _iso(now - timedelta(days=pending_days_ago)) if pending_days_ago is not None else None,
        "last_status_change_date": None,
        "last_update_date": _iso(now - timedelta(hours=1)),
        "price_per_sqft": None,
        "tags": tags,
        "details": details,
        "flags": {"is_pending": is_pending, "is_contingent": is_contingent, "is_new_construction": False},
        "description": {
            "type": style,
            "sqft": sqft,
            "beds": beds,
            "baths_full": 2 if beds else None,
            "baths_half": None,
            "lot_sqft": lot_sqft,
            "year_built": 2005 if sqft else None,
            "garage": None,
            "name": None,
            "stories": None,
            "text": text,
        },
        "source": {"id": "FLMLS", "listing_id": f"MLS{property_id}"},
        "hoa": None,
        "location": {
            "address": {
                "street_direction": None,
                "street_number": street_number,
                "street_name": street_name,
                "street_suffix": street_suffix,
                "line": line,
                "unit": None,
                "city": city,
                "state_code": state_code,
                "postal_code": postal_code,
                "coordinate": {"lat": lat, "lon": lon} if lat is not None else None,
            },
            "county": {"name": county, "fips_code": "12071"},
            "neighborhoods": [{"name": "Westwood"}],
        },
        "tax_record": None,
        "primary_photo": {"href": f"https://ap.rdcpix.com/{property_id}s.jpg"},
        "photos": None,
        "advertisers": advertisers,
        "current_estimates": None,
        "open_houses": None,
        "pet_policy": None,
        "units": None,
    }


ADDRESS_LOCATION = {
    "text": "100 Main St, Lehigh Acres, FL 33971",
    "geo": {"area_type": "address", "mpr_id": "1", "city": "Lehigh Acres", "state_code": "FL",
            "postal_code": "33971", "county": "Lee", "centroid": {"lat": 26.6, "lon": -81.6}},
}


AREA_LOCATION = {
    "text": "Lehigh Acres, FL",
    "geo": {
        "area_type": "city",
        "city": "Lehigh Acres",
        "state_code": "FL",
        "postal_code": None,
        "county": "Lee",
        "centroid": {"lat": 26.6, "lon": -81.6},
    },
}


class FakeRealtorAPI:
    """Callable installed in place of RealtorScraper._graphql_post."""

    def __init__(self, homes: list[dict] | None = None, location: dict | None = None,
                 details: dict | None = None, details_error: dict | None = None, aggregation: dict | None = None):
        self.homes = homes or []
        self.location = location or AREA_LOCATION
        self.details = details or {}
        self.details_error = details_error
        self.aggregation = aggregation
        self.calls: list[tuple[str, str, dict]] = []
        self.schema_errors: list[str] = []

    # -- helpers --------------------------------------------------------
    def queries(self, operation_name: str) -> list[str]:
        return [q for op, q, _ in self.calls if op == operation_name]

    @staticmethod
    def _statuses(query: str) -> list[str]:
        match = re.search(r"status:\s*\[([a-z_,\s]+)\]", query) or re.search(r"status:\s*([a-z_]+)", query)
        if not match:
            return []
        return [s.strip() for s in match.group(1).split(",") if s.strip()]

    def _search_results(self, query: str) -> list[dict]:
        statuses = self._statuses(query)
        pending_only = "or_filters" in query and "pending: true" in query
        results = []
        for home in self.homes:
            if statuses and home["status"] not in statuses:
                continue
            if pending_only and not (home["flags"]["is_pending"] or home["flags"]["is_contingent"]):
                continue
            results.append(deepcopy(home))
        return results

    # -- the fake transport ----------------------------------------------
    def __call__(self, scraper, query: str, variables: dict, operation_name: str) -> dict:
        self.calls.append((operation_name, query, deepcopy(variables)))

        if operation_name == "Search_suggestions":
            return {"data": {"search_suggestions": {"geo_results": [self.location]}}}

        # Validate everything else against the bundled schema
        errors = validate_query(query)
        self.schema_errors.extend(f"{operation_name}: {e}" for e in errors)

        if operation_name == "GetHomeSearch":
            results = self._search_results(query)
            offset = variables.get("offset", 0)
            page = results[offset:offset + 200]
            return {"data": {"homeSearch": {"__typename": "SearchHomeResult", "count": len(page),
                                            "total": len(results), "results": page}}}

        if operation_name == "GetHomeDetails":
            pid = variables.get("property_id")
            home = next((h for h in self.homes if h["property_id"] == pid), None)
            return {"data": {"home": deepcopy(home)}}

        if operation_name == "GetPropertyDetails":
            if self.details_error:
                return deepcopy(self.details_error)
            ids = re.findall(r'home_([A-Za-z0-9_]+): home\(', query)
            return {"data": {f"home_{pid}": deepcopy(self.details.get(pid)) for pid in ids}}

        if operation_name == "GetLocationAggregates":
            return {"data": {"home_search": {"total": 1, "aggregation": deepcopy(self.aggregation)}}}

        raise AssertionError(f"Unexpected operation {operation_name}")
