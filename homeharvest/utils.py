from __future__ import annotations
import pandas as pd
import warnings
from datetime import datetime
from .core.scrapers.models import Property, ListingType, Advertisers
from .exceptions import InvalidListingType, InvalidDate

ordered_properties = [
    "property_url",
    "property_id",
    "listing_id",
    "permalink",
    "mls",
    "mls_id",
    "status",
    "mls_status",
    "text",
    "style",
    "formatted_address",
    "full_street_line",
    "street",
    "unit",
    "city",
    "state",
    "zip_code",
    "beds",
    "full_baths",
    "half_baths",
    "sqft",
    "year_built",
    "days_on_mls",
    "list_price",
    "list_price_min",
    "list_price_max",
    "list_date",
    "pending_date",
    "sold_price",
    "last_sold_date",
    "last_sold_price",
    "last_status_change_date",
    "last_update_date",
    "assessed_value",
    "estimated_value",
    "tax",
    "tax_history",
    "new_construction",
    "lot_sqft",
    "price_per_sqft",
    "latitude",
    "longitude",
    "neighborhoods",
    "county",
    "fips_code",
    "stories",
    "hoa_fee",
    "parking_garage",
    "agent_id",
    "agent_name",
    "agent_email",
    "agent_phones",
    "agent_mls_set",
    "agent_nrds_id",
    "broker_id",
    "broker_name",
    "builder_id",
    "builder_name",
    "office_id",
    "office_mls_set",
    "office_name",
    "office_email",
    "office_phones",
    "buyer_agent_id",
    "buyer_agent_name",
    "buyer_agent_email",
    "buyer_agent_phones",
    "buyer_broker_name",
    "buyer_office_name",
    "buyer_office_phones",
    "nearby_schools",
    "primary_photo",
    "alt_photos",
    "tags",
]

#: Columns only added to the DataFrame when the matching option is used on scrape_property()
OPTIONAL_COLUMNS = {
    "features": [
        "matched_features",
        "matched_keywords",
        "feature_evidence",
    ],
    "property_history": [
        "original_list_price",
        "last_price_change_amount",
        "last_price_change_date",
        "property_history",
    ],
    "environmental_risk": [
        "flood_factor_score",
        "flood_factor_severity",
        "flood_fema_zone",
        "flood_insurance_requirement",
        "fire_factor_score",
        "fire_factor_severity",
        "heat_factor_score",
        "heat_factor_severity",
        "wind_factor_score",
        "wind_factor_severity",
        "air_factor_score",
        "air_factor_severity",
        "noise_score",
        "environmental_risk_url",
    ],
    "insurance": [
        "home_insurance_monthly",
        "insurance_rate",
        "property_tax_rate",
        "property_tax_monthly",
        "estimated_monthly_payment",
        "flood_insurance_requirement",
        "flood_insurance_quotes",
        "fire_insurance_quotes",
    ],
    "market_stats": [
        "estimated_rent",
        "estimated_rent_low",
        "estimated_rent_high",
        "neighborhood_median_rent_price",
        "neighborhood_median_listing_price",
        "neighborhood_median_sold_price",
        "neighborhood_median_price_per_sqft",
        "neighborhood_median_days_on_market",
        "zip_median_rent_price",
        "zip_median_listing_price",
        "zip_median_sold_price",
    ],
}


def get_columns(extra_groups=None) -> list[str]:
    """Base DataFrame columns plus any optional column groups (see OPTIONAL_COLUMNS)."""
    columns = list(ordered_properties)
    for group in extra_groups or []:
        for column in OPTIONAL_COLUMNS.get(group, []):
            if column not in columns:
                columns.append(column)
    return columns


def process_result(result: Property, extra_groups=None) -> pd.DataFrame:
    columns = get_columns(extra_groups)
    prop_data = {prop: None for prop in columns}
    prop_data.update(result.model_dump())

    if "address" in prop_data and prop_data["address"]:
        address_data = prop_data["address"]
        prop_data["full_street_line"] = address_data.get("full_line")
        prop_data["street"] = address_data.get("street")
        prop_data["unit"] = address_data.get("unit")
        prop_data["city"] = address_data.get("city")
        prop_data["state"] = address_data.get("state")
        prop_data["zip_code"] = address_data.get("zip")
        prop_data["formatted_address"] = address_data.get("formatted_address")

    if "advertisers" in prop_data and prop_data.get("advertisers"):
        advertiser_data = prop_data["advertisers"]
        if advertiser_data.get("agent"):
            agent_data = advertiser_data["agent"]
            prop_data["agent_id"] = agent_data.get("uuid")
            prop_data["agent_name"] = agent_data.get("name")
            prop_data["agent_email"] = agent_data.get("email")
            prop_data["agent_phones"] = agent_data.get("phones")
            prop_data["agent_mls_set"] = agent_data.get("mls_set")
            prop_data["agent_nrds_id"] = agent_data.get("nrds_id")

        if advertiser_data.get("broker"):
            broker_data = advertiser_data["broker"]
            prop_data["broker_id"] = broker_data.get("uuid")
            prop_data["broker_name"] = broker_data.get("name")

        if advertiser_data.get("builder"):
            builder_data = advertiser_data["builder"]
            prop_data["builder_id"] = builder_data.get("uuid")
            prop_data["builder_name"] = builder_data.get("name")

        if advertiser_data.get("office"):
            office_data = advertiser_data["office"]
            prop_data["office_id"] = office_data.get("uuid")
            prop_data["office_name"] = office_data.get("name")
            prop_data["office_email"] = office_data.get("email")
            prop_data["office_phones"] = office_data.get("phones")
            prop_data["office_mls_set"] = office_data.get("mls_set")

        if advertiser_data.get("buyer_agent"):
            buyer_agent = advertiser_data["buyer_agent"]
            prop_data["buyer_agent_id"] = buyer_agent.get("uuid")
            prop_data["buyer_agent_name"] = buyer_agent.get("name")
            prop_data["buyer_agent_email"] = buyer_agent.get("email")
            prop_data["buyer_agent_phones"] = buyer_agent.get("phones")

        if advertiser_data.get("buyer_broker"):
            prop_data["buyer_broker_name"] = advertiser_data["buyer_broker"].get("name")

        if advertiser_data.get("buyer_office"):
            prop_data["buyer_office_name"] = advertiser_data["buyer_office"].get("name")
            prop_data["buyer_office_phones"] = advertiser_data["buyer_office"].get("phones")

    prop_data["tags"] = ", ".join(result.tags) if result.tags else None
    _flatten_optional_groups(result, prop_data, extra_groups or [])

    prop_data["price_per_sqft"] = prop_data["prc_sqft"]
    prop_data["nearby_schools"] = filter(None, prop_data["nearby_schools"]) if prop_data["nearby_schools"] else None
    prop_data["nearby_schools"] = ", ".join(set(prop_data["nearby_schools"])) if prop_data["nearby_schools"] else None
    
    # Convert datetime objects to strings for CSV (preserve full datetime including time)
    for date_field in ["list_date", "pending_date", "last_sold_date", "last_status_change_date"]:
        if prop_data.get(date_field):
            prop_data[date_field] = prop_data[date_field].strftime("%Y-%m-%d %H:%M:%S") if hasattr(prop_data[date_field], 'strftime') else prop_data[date_field]
    
    # Convert HttpUrl objects to strings for CSV
    if prop_data.get("property_url"):
        prop_data["property_url"] = str(prop_data["property_url"])

    description = result.description
    if description:
        prop_data["primary_photo"] = str(description.primary_photo) if description.primary_photo else None
        prop_data["alt_photos"] = ", ".join(str(url) for url in description.alt_photos) if description.alt_photos else None
        prop_data["style"] = (
            description.style
            if isinstance(description.style, str)
            else description.style.value if description.style else None
        )
        prop_data["beds"] = description.beds
        prop_data["full_baths"] = description.baths_full
        prop_data["half_baths"] = description.baths_half
        prop_data["sqft"] = description.sqft
        prop_data["lot_sqft"] = description.lot_sqft
        prop_data["sold_price"] = description.sold_price
        prop_data["year_built"] = description.year_built
        prop_data["parking_garage"] = description.garage
        prop_data["stories"] = description.stories
        prop_data["text"] = description.text

    properties_df = pd.DataFrame([prop_data])
    properties_df = properties_df.reindex(columns=columns)

    return properties_df[columns]


def _risk_columns(prop_data: dict, prefix: str, risk) -> None:
    if risk is not None:
        prop_data[f"{prefix}_factor_score"] = risk.score
        prop_data[f"{prefix}_factor_severity"] = risk.severity


def _market_columns(prop_data: dict, prefix: str, stats, fields) -> None:
    if stats is not None:
        for field in fields:
            prop_data[f"{prefix}_{field}"] = getattr(stats, field)


def _flatten_optional_groups(result: Property, prop_data: dict, extra_groups) -> None:
    """Flatten nested opt-in data (features, history, risk, insurance, market stats) into columns."""
    if "features" in extra_groups:
        prop_data["matched_features"] = ", ".join(result.matched_features) if result.matched_features else None
        prop_data["matched_keywords"] = ", ".join(result.matched_keywords) if result.matched_keywords else None
        prop_data["feature_evidence"] = (
            "; ".join(f"{k}: {v}" for k, v in result.feature_evidence.items()) if result.feature_evidence else None
        )

    if "property_history" in extra_groups:
        prop_data["property_history"] = (
            [event.model_dump() for event in result.property_history] if result.property_history else None
        )
        if result.last_price_change_date:
            prop_data["last_price_change_date"] = result.last_price_change_date.strftime("%Y-%m-%d %H:%M:%S")

    if "environmental_risk" in extra_groups and result.environmental_risk:
        risk = result.environmental_risk
        _risk_columns(prop_data, "flood", risk.flood)
        _risk_columns(prop_data, "fire", risk.fire)
        _risk_columns(prop_data, "heat", risk.heat)
        _risk_columns(prop_data, "wind", risk.wind)
        _risk_columns(prop_data, "air", risk.air)
        if risk.flood:
            prop_data["flood_fema_zone"] = ", ".join(risk.flood.fema_zone) if risk.flood.fema_zone else None
            prop_data["flood_insurance_requirement"] = risk.flood.insurance_requirement
        prop_data["noise_score"] = risk.noise_score
        prop_data["environmental_risk_url"] = next(
            (r.report_url for r in (risk.flood, risk.fire, risk.heat, risk.wind, risk.air) if r and r.report_url),
            None,
        )

    if "insurance" in extra_groups and result.insurance:
        insurance = result.insurance
        prop_data["home_insurance_monthly"] = insurance.home_insurance_monthly
        prop_data["insurance_rate"] = insurance.insurance_rate
        prop_data["property_tax_rate"] = insurance.property_tax_rate
        prop_data["property_tax_monthly"] = insurance.property_tax_monthly
        prop_data["estimated_monthly_payment"] = insurance.estimated_monthly_payment
        prop_data["flood_insurance_quotes"] = (
            [q.model_dump() for q in insurance.flood_insurance_quotes] if insurance.flood_insurance_quotes else None
        )
        prop_data["fire_insurance_quotes"] = (
            [q.model_dump() for q in insurance.fire_insurance_quotes] if insurance.fire_insurance_quotes else None
        )
        if insurance.flood_insurance_requirement:
            prop_data["flood_insurance_requirement"] = insurance.flood_insurance_requirement

    if "market_stats" in extra_groups and result.market_stats:
        market = result.market_stats
        prop_data["estimated_rent"] = market.estimated_rent
        prop_data["estimated_rent_low"] = market.estimated_rent_low
        prop_data["estimated_rent_high"] = market.estimated_rent_high
        _market_columns(
            prop_data, "neighborhood", market.neighborhood,
            ["median_rent_price", "median_listing_price", "median_sold_price",
             "median_price_per_sqft", "median_days_on_market"],
        )
        _market_columns(
            prop_data, "zip", market.zip_code,
            ["median_rent_price", "median_listing_price", "median_sold_price"],
        )


def validate_input(listing_type: str | list[str] | None) -> None:
    if listing_type is None:
        return  # None is valid - returns all types

    if isinstance(listing_type, list):
        for lt in listing_type:
            if lt.upper() not in ListingType.__members__:
                raise InvalidListingType(f"Provided listing type, '{lt}', does not exist.")
    else:
        if listing_type.upper() not in ListingType.__members__:
            raise InvalidListingType(f"Provided listing type, '{listing_type}', does not exist.")


def validate_dates(date_from: str | None, date_to: str | None) -> None:
    # Allow either date_from or date_to individually, or both together
    try:
        # Validate and parse date_from if provided
        date_from_obj = None
        if date_from:
            date_from_str = date_from.replace('Z', '+00:00') if date_from.endswith('Z') else date_from
            date_from_obj = datetime.fromisoformat(date_from_str)

        # Validate and parse date_to if provided
        date_to_obj = None
        if date_to:
            date_to_str = date_to.replace('Z', '+00:00') if date_to.endswith('Z') else date_to
            date_to_obj = datetime.fromisoformat(date_to_str)

        # If both provided, ensure date_to is after date_from
        if date_from_obj and date_to_obj and date_to_obj < date_from_obj:
            raise InvalidDate(f"date_to ('{date_to}') must be after date_from ('{date_from}').")

    except ValueError as e:
        # Provide specific guidance on the expected format
        raise InvalidDate(
            f"Invalid date format. Expected ISO 8601 format. "
            f"Examples: '2025-01-20' (date only) or '2025-01-20T14:30:00' (with time). "
            f"Got: date_from='{date_from}', date_to='{date_to}'. Error: {e}"
        )


def validate_limit(limit: int) -> None:
    #: 1 -> 10000 limit

    if limit is not None and (limit < 1 or limit > 10000):
        raise ValueError("Property limit must be between 1 and 10,000.")


def validate_offset(offset: int, limit: int = 10000) -> None:
    """Validate offset parameter for pagination.

    Args:
        offset: Starting position for results pagination
        limit: Maximum number of results to fetch

    Raises:
        ValueError: If offset is invalid or if offset + limit exceeds API limit
    """
    if offset is not None and offset < 0:
        raise ValueError("Offset must be non-negative (>= 0).")

    # Check if offset + limit exceeds API's hard limit of 10,000
    if offset is not None and limit is not None and (offset + limit) > 10000:
        raise ValueError(
            f"offset ({offset}) + limit ({limit}) = {offset + limit} exceeds API maximum of 10,000. "
            f"The API cannot return results beyond position 10,000. "
            f"To fetch more results, narrow your search."
        )

    # Warn if offset is not a multiple of 200 (API page size)
    if offset is not None and offset > 0 and offset % 200 != 0:
        warnings.warn(
            f"Offset should be a multiple of 200 (page size) for optimal performance. "
            f"Using offset {offset} may result in less efficient pagination.",
            UserWarning
        )


def validate_datetime(datetime_value) -> None:
    """Validate datetime value (accepts datetime objects or ISO 8601 strings)."""
    if datetime_value is None:
        return

    # Already a datetime object - valid
    from datetime import datetime as dt, date
    if isinstance(datetime_value, (dt, date)):
        return

    # Must be a string - validate ISO 8601 format
    if not isinstance(datetime_value, str):
        raise InvalidDate(
            f"Invalid datetime value. Expected datetime object, date object, or ISO 8601 string. "
            f"Got: {type(datetime_value).__name__}"
        )

    try:
        # Try parsing as ISO 8601 datetime
        datetime.fromisoformat(datetime_value.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        raise InvalidDate(
            f"Invalid datetime format: '{datetime_value}'. "
            f"Expected ISO 8601 format (e.g., '2025-01-20T14:30:00' or '2025-01-20')."
        )


def validate_last_update_filters(updated_since: str | None, updated_in_past_hours: int | None) -> None:
    """Validate last_update_date filtering parameters."""
    if updated_since and updated_in_past_hours:
        raise ValueError(
            "Cannot use both 'updated_since' and 'updated_in_past_hours' parameters together. "
            "Please use only one method to filter by last_update_date."
        )

    # Validate updated_since format if provided
    if updated_since:
        validate_datetime(updated_since)

    # Validate updated_in_past_hours range if provided
    if updated_in_past_hours is not None:
        if updated_in_past_hours < 1:
            raise ValueError(
                f"updated_in_past_hours must be at least 1. Got: {updated_in_past_hours}"
            )


def validate_filters(
    beds_min: int | None = None,
    beds_max: int | None = None,
    baths_min: float | None = None,
    baths_max: float | None = None,
    sqft_min: int | None = None,
    sqft_max: int | None = None,
    price_min: int | None = None,
    price_max: int | None = None,
    lot_sqft_min: int | None = None,
    lot_sqft_max: int | None = None,
    year_built_min: int | None = None,
    year_built_max: int | None = None,
) -> None:
    """Validate that min values are less than max values for range filters."""
    ranges = [
        ("beds", beds_min, beds_max),
        ("baths", baths_min, baths_max),
        ("sqft", sqft_min, sqft_max),
        ("price", price_min, price_max),
        ("lot_sqft", lot_sqft_min, lot_sqft_max),
        ("year_built", year_built_min, year_built_max),
    ]

    for name, min_val, max_val in ranges:
        if min_val is not None and max_val is not None and min_val > max_val:
            raise ValueError(f"{name}_min ({min_val}) cannot be greater than {name}_max ({max_val}).")


def validate_sort(sort_by: str | None, sort_direction: str | None = "desc") -> None:
    """Validate sort parameters."""
    valid_sort_fields = ["list_date", "sold_date", "list_price", "sqft", "beds", "baths", "last_update_date"]
    valid_directions = ["asc", "desc"]

    if sort_by and sort_by not in valid_sort_fields:
        raise ValueError(
            f"Invalid sort_by value: '{sort_by}'. "
            f"Valid options: {', '.join(valid_sort_fields)}"
        )

    if sort_direction and sort_direction not in valid_directions:
        raise ValueError(
            f"Invalid sort_direction value: '{sort_direction}'. "
            f"Valid options: {', '.join(valid_directions)}"
        )


def convert_to_datetime_string(value) -> str | None:
    """
    Convert datetime object or string to ISO 8601 string format with UTC timezone.

    Accepts:
    - datetime.datetime objects (naive or timezone-aware)
      - Naive datetimes are treated as local time and converted to UTC
      - Timezone-aware datetimes are converted to UTC
    - datetime.date objects (treated as midnight UTC)
    - ISO 8601 strings (returned as-is)
    - None (returns None)

    Returns ISO 8601 formatted string with UTC timezone or None.

    Examples:
        >>> # Naive datetime (treated as local time)
        >>> convert_to_datetime_string(datetime(2025, 1, 20, 14, 30))
        '2025-01-20T22:30:00+00:00'  # Assuming PST (UTC-8)

        >>> # Timezone-aware datetime
        >>> convert_to_datetime_string(datetime(2025, 1, 20, 14, 30, tzinfo=timezone.utc))
        '2025-01-20T14:30:00+00:00'
    """
    if value is None:
        return None

    # Already a string - return as-is
    if isinstance(value, str):
        return value

    # datetime.datetime object
    from datetime import datetime, date, timezone
    if isinstance(value, datetime):
        # Handle naive datetime - treat as local time and convert to UTC
        if value.tzinfo is None:
            # Convert naive datetime to aware local time, then to UTC
            local_aware = value.astimezone()
            utc_aware = local_aware.astimezone(timezone.utc)
            return utc_aware.isoformat()
        else:
            # Already timezone-aware, convert to UTC
            utc_aware = value.astimezone(timezone.utc)
            return utc_aware.isoformat()

    # datetime.date object (convert to datetime at midnight UTC)
    if isinstance(value, date):
        utc_datetime = datetime.combine(value, datetime.min.time()).replace(tzinfo=timezone.utc)
        return utc_datetime.isoformat()

    raise ValueError(
        f"Invalid datetime value. Expected datetime object, date object, or ISO 8601 string. "
        f"Got: {type(value).__name__}"
    )


def extract_timedelta_hours(value) -> int | None:
    """
    Extract hours from int or timedelta object.

    Accepts:
    - int (returned as-is)
    - timedelta objects (converted to total hours)
    - None (returns None)

    Returns integer hours or None.
    """
    if value is None:
        return None

    # Already an int - return as-is
    if isinstance(value, int):
        return value

    # timedelta object - convert to hours
    from datetime import timedelta
    if isinstance(value, timedelta):
        return int(value.total_seconds() / 3600)

    raise ValueError(
        f"Invalid past_hours value. Expected int or timedelta object. "
        f"Got: {type(value).__name__}"
    )


def extract_timedelta_days(value) -> int | None:
    """
    Extract days from int or timedelta object.

    Accepts:
    - int (returned as-is)
    - timedelta objects (converted to total days)
    - None (returns None)

    Returns integer days or None.
    """
    if value is None:
        return None

    # Already an int - return as-is
    if isinstance(value, int):
        return value

    # timedelta object - convert to days
    from datetime import timedelta
    if isinstance(value, timedelta):
        return int(value.total_seconds() / 86400)  # 86400 seconds in a day

    raise ValueError(
        f"Invalid past_days value. Expected int or timedelta object. "
        f"Got: {type(value).__name__}"
    )


def detect_precision_and_convert(value):
    """
    Detect if input has time precision and convert to ISO string.

    Accepts:
    - datetime.datetime objects → (ISO string, "hour")
    - datetime.date objects → (ISO string at midnight, "day")
    - ISO 8601 datetime strings with time → (string as-is, "hour")
    - Date-only strings "YYYY-MM-DD" → (string as-is, "day")
    - None → (None, None)

    Returns:
        tuple: (iso_string, precision) where precision is "day" or "hour"
    """
    if value is None:
        return (None, None)

    from datetime import datetime as dt, date

    # datetime.datetime object - has time precision
    if isinstance(value, dt):
        return (value.isoformat(), "hour")

    # datetime.date object - day precision only
    if isinstance(value, date):
        # Convert to datetime at midnight
        return (dt.combine(value, dt.min.time()).isoformat(), "day")

    # String - detect if it has time component
    if isinstance(value, str):
        # ISO 8601 datetime with time component (has 'T' and time)
        if 'T' in value:
            return (value, "hour")
        # Date-only string
        else:
            return (value, "day")

    raise ValueError(
        f"Invalid date value. Expected datetime object, date object, or ISO 8601 string. "
        f"Got: {type(value).__name__}"
    )
