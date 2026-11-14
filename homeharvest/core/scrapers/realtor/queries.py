SEARCH_RESULTS_FRAGMENT = """
fragment PropertyResult on SearchHome {
    __typename
    pending_date
    listing_id
    property_id
    href
    permalink
    list_date
    status
    mls_status
    last_sold_price
    last_sold_date
    last_status_change_date
    last_update_date
    list_price
    list_price_max
    list_price_min
    price_per_sqft
    tags
    open_houses {
        start_date
        end_date
        description
        time_zone
        dst
        href
        methods
    }
    details {
        category
        text
        parent_category
    }
    pet_policy {
        cats
        dogs
        dogs_small
        dogs_large
        __typename
    }
    units {
        availability {
          date
          __typename
        }
        description {
          baths_consolidated
          baths
          beds
          sqft
          __typename
        }
        photos(https: true) {
            title
            href
            tags {
                label
            }
        }
        list_price
        __typename
    }
    flags {
        is_contingent
        is_pending
        is_new_construction
    }
    description {
        type
        sqft
        beds
        baths_full
        baths_half
        lot_sqft
        year_built
        garage
        type
        name
        stories
        text
    }
    source {
        id
        listing_id
    }
    hoa {
        fee
    }
    location {
        address {
            street_direction
            street_number
            street_name
            street_suffix
            line
            unit
            city
            state_code
            postal_code
            coordinate {
                lon
                lat
            }
        }
        county {
            name
            fips_code
        }
        neighborhoods {
            name
        }
    }
    tax_record {
        cl_id
        public_record_id
        last_update_date
        apn
        tax_parcel_id
    }
    primary_photo(https: true) {
        href
    }
    advertisers {
        email
        broker {
            name
            fulfillment_id
        }
        type
        name
        fulfillment_id
        builder {
            name
            fulfillment_id
        }
        phones {
            ext
            primary
            type
            number
        }
        office {
            name
            email
            fulfillment_id
            href
            phones {
                number
                type
                primary
                ext
            }
            mls_set
        }
        corporation {
            specialties
            name
            bio
            href
            fulfillment_id
        }
        mls_set
        nrds_id
        state_license
        rental_corporation {
            fulfillment_id
        }
        rental_management {
            name
            href
            fulfillment_id
        }
    }
    current_estimates {
        __typename
        source {
            __typename
            type
            name
        }
        estimate
        estimateHigh: estimate_high
        estimateLow: estimate_low
        date
        isBestHomeValue: isbest_homevalue
    }
}
"""

_SEARCH_HOMES_DATA_BASE = """{
    pending_date
    listing_id
    property_id
    href
    permalink
    list_date
    status
    mls_status
    last_sold_price
    last_sold_date
    last_status_change_date
    last_update_date
    list_price
    list_price_max
    list_price_min
    price_per_sqft
    tags
    open_houses {
        start_date
        end_date
        description
        time_zone
        dst
        href
        methods
    }
    details {
        category
        text
        parent_category
    }
    pet_policy {
        cats
        dogs
        dogs_small
        dogs_large
        __typename
    }
    units {
        availability {
          date
          __typename
        }
        description {
          baths_consolidated
          baths
          beds
          sqft
          __typename
        }
        photos(https: true) {
            title
            href
            tags {
                label
            }
        }
        list_price
        __typename
    }
    flags {
        is_contingent
        is_pending
        is_new_construction
    }
    description {
        type
        sqft
        beds
        baths_full
        baths_half
        lot_sqft
        year_built
        garage
        type
        name
        stories
        text
    }
    source {
        id
        listing_id
    }
    hoa {
        fee
    }
    location {
        address {
            street_direction
            street_number
            street_name
            street_suffix
            line
            unit
            city
            state_code
            postal_code
            coordinate {
                lon
                lat
            }
        }
        county {
            name
            fips_code
        }
        neighborhoods {
            name
        }
    }
    tax_record {
        cl_id
        public_record_id
        last_update_date
        apn
        tax_parcel_id
    }
    primary_photo(https: true) {
        href
    }
    photos(https: true) {
        title
        href
        tags {
            label
        }
    }
    advertisers {
        email
        broker {
            name
            fulfillment_id
        }
        type
        name
        fulfillment_id
        builder {
            name
            fulfillment_id
        }
        phones {
            ext
            primary
            type
            number
        }
        office {
            name
            email
            fulfillment_id
            href
            phones {
                number
                type
                primary
                ext
            }
            mls_set
        }
        corporation {
            specialties
            name
            bio
            href
            fulfillment_id
        }
        mls_set
        nrds_id
        state_license
        rental_corporation {
            fulfillment_id
        }
        rental_management {
            name
            href
            fulfillment_id
        }
    }
    """


HOME_FRAGMENT = """
fragment PropertyResult on Home {
    __typename
    pending_date
    listing_id
    property_id
    href
    permalink
    list_date
    status
    mls_status
    last_sold_price
    last_sold_date
    last_status_change_date
    last_update_date
    list_price
    list_price_max
    list_price_min
    price_per_sqft
    tags
    open_houses {
        start_date
        end_date
        description
        time_zone
        dst
        href
        methods
    }
    details {
        category
        text
        parent_category
    }
    pet_policy {
        cats
        dogs
        dogs_small
        dogs_large
        __typename
    }
    units {
        availability {
          date
          __typename
        }
        description {
          baths_consolidated
          baths
          beds
          sqft
          __typename
        }
        photos(https: true) {
            title
            href
            tags {
                label
            }
        }
        list_price
        __typename
    }
    flags {
        is_contingent
        is_pending
        is_new_construction
    }
    description {
        type
        sqft
        beds
        baths_full
        baths_half
        lot_sqft
        year_built
        garage
        type
        name
        stories
        text
    }
    source {
        id
        listing_id
    }
    hoa {
        fee
    }
    location {
        address {
            street_direction
            street_number
            street_name
            street_suffix
            line
            unit
            city
            state_code
            postal_code
            coordinate {
                lon
                lat
            }
        }
        county {
            name
            fips_code
        }
        neighborhoods {
            name
        }
        parcel {
            parcel_id
        }
    }
    tax_record {
        cl_id
        public_record_id
        last_update_date
        apn
        tax_parcel_id
    }
    primary_photo(https: true) {
        href
    }
    photos(https: true) {
        title
        href
        tags {
            label
        }
    }
    advertisers {
        email
        broker {
            name
            fulfillment_id
        }
        type
        name
        fulfillment_id
        builder {
            name
            fulfillment_id
        }
        phones {
            ext
            primary
            type
            number
        }
        office {
            name
            email
            fulfillment_id
            href
            phones {
                number
                type
                primary
                ext
            }
            mls_set
        }
        corporation {
            specialties
            name
            bio
            href
            fulfillment_id
        }
        mls_set
        nrds_id
        state_license
        rental_corporation {
            fulfillment_id
        }
        rental_management {
            name
            href
            fulfillment_id
        }
    }
    nearbySchools: nearby_schools(radius: 5.0, limit_per_level: 3) {
        __typename schools { district { __typename id name } }
    }
    popularity {
        periods {
            clicks_total
            views_total
            dwell_time_mean
            dwell_time_median
            leads_total
            shares_total
            saves_total
            last_n_days
        }
    }
    taxHistory: tax_history { __typename tax year assessment { __typename building land total } }
    property_history {
        date
        event_name
        price
    }
    monthly_fees {
        description
        display_amount
    }
    one_time_fees {
        description
        display_amount
    }
    parking {
        unassigned_space_rent
        assigned_spaces_available
        description
        assigned_space_rent
    }
    terms {
        text
        category
    }
    estimates {
        __typename
        currentValues: current_values {
            __typename
            source { __typename type name }
            estimate
            estimateHigh: estimate_high
            estimateLow: estimate_low
            date
            isBestHomeValue: isbest_homevalue
        }
    }
}
"""

HOMES_DATA = """%s
                nearbySchools: nearby_schools(radius: 5.0, limit_per_level: 3) {
                            __typename schools { district { __typename id name } }
                        }
                monthly_fees {
                    description
                    display_amount
                }
                one_time_fees {
                    description
                    display_amount
                }
                popularity {
                    periods {
                        clicks_total
                        views_total
                        dwell_time_mean
                        dwell_time_median
                        leads_total
                        shares_total
                        saves_total
                        last_n_days
                    }
                }
                location {
                    parcel {
                        parcel_id
                    }
                }
                parking {
                    unassigned_space_rent
                    assigned_spaces_available
                    description
                    assigned_space_rent
                }
                terms {
                    text
                    category
                }
                taxHistory: tax_history { __typename tax year assessment { __typename building land total } }
                estimates {
                    __typename
                    currentValues: current_values {
                        __typename
                        source { __typename type name }
                        estimate
                        estimateHigh: estimate_high
                        estimateLow: estimate_low
                        date
                        isBestHomeValue: isbest_homevalue
                    }
                }
}""" % _SEARCH_HOMES_DATA_BASE

SEARCH_HOMES_DATA = """%s
current_estimates {
    __typename
    source {
        __typename
        type
        name
    }
    estimate
    estimateHigh: estimate_high
    estimateLow: estimate_low
    date
    isBestHomeValue: isbest_homevalue
}
}""" % _SEARCH_HOMES_DATA_BASE

# Query body using inline fields (kept for backward compatibility)
GENERAL_RESULTS_QUERY_BODY = """{
                            count
                            total
                            results %s
                        }""" % SEARCH_HOMES_DATA

GENERAL_RESULTS_QUERY = """{
                            __typename
                            count
                            total
                            results %s
                        }""" % SEARCH_HOMES_DATA

LISTING_PHOTOS_FRAGMENT = """
fragment ListingPhotosFragment on SearchHome {
    __typename
    photos(https: true) {
        __typename
        title
        href
        tags {
            __typename
            label
            probability
        }
    }
}
"""

SEARCH_SUGGESTIONS_QUERY = """query Search_suggestions($searchInput: SearchSuggestionsInput!) {
  search_suggestions(search_input: $searchInput) {
    raw_input_parser_result
    typeahead_results {
      display_string
      display_geo
      geo {
        _id
        _score
        mpr_id
        area_type
        city
        state_code
        state
        postal_code
        country
        lat
        lon
        county
        counties {
          name
          fips
          state_code
        }
        slug_id
        geo_id
        score
        name
        city_slug_id
        centroid {
          lat
          lon
        }
        county_needed_for_uniq
        street
        line
        school
        school_id
        school_district
        has_catchment
        university
        university_id
        neighborhood
        park
      }
      url
    }
    geo_results {
      type
      text
      geo {
        _id
        _score
        mpr_id
        area_type
        city
        state_code
        state
        postal_code
        country
        lat
        lon
        county
        counties {
          name
          fips
          state_code
        }
        slug_id
        geo_id
        score
        name
        city_slug_id
        centroid {
          lat
          lon
        }
        county_needed_for_uniq
        street
        line
        school
        school_id
        school_district
        has_catchment
        university
        university_id
        neighborhood
        park
      }
    }
    no_matches
    has_results
    original_string
  }
}"""


# ---------------------------------------------------------------------------
# Opt-in property detail enrichment
#
# These selections are only requested when the matching include_* flag is set
# on scrape_property(). They are fetched with aliased `home(property_id: ...)`
# queries in small batches, so the main search query is never affected.
# ---------------------------------------------------------------------------

_INSURANCE_QUOTE_FIELDS = """
    provider_name
    provider_url
    price
    min_price
    max_price
    home_coverage
    contents_coverage
    expires
"""

_HOUSING_MARKET_FIELDS = """
    geo_statistics {
        housing_market {
            median_rent_price
            median_listing_price
            median_sold_price
            median_price_per_sqft
            median_days_on_market
        }
    }
"""

#: Homeowner's insurance estimate + flood/fire insurance quotes (issue #121)
DETAIL_INSURANCE_FIELDS = """
    mortgage {
        insurance_rate
        property_tax_rate
        estimate {
            monthly_payment
            monthly_payment_details {
                type
                amount
                display_name
            }
        }
    }
    local {
        flood {
            insurance_requirement
            insurance_rates { %s }
        }
        wildfire {
            insurance_rates { %s }
        }
    }
""" % (_INSURANCE_QUOTE_FIELDS, _INSURANCE_QUOTE_FIELDS)

#: Rent estimate + neighborhood / ZIP medians (issue #137)
DETAIL_MARKET_STATS_FIELDS = """
    rent_amount_model {
        value
        value_low
        value_high
    }
    location {
        neighborhoods {
            name
            %s
        }
        postal_code {
            postal_code
            %s
        }
    }
""" % (_HOUSING_MARKET_FIELDS, _HOUSING_MARKET_FIELDS)


_ADVERTISER_PHONE_FIELDS = "phones { number type primary ext }"

#: Listing / sale history, price changes and the buyer side of past sales
DETAIL_PROPERTY_HISTORY_FIELDS = """
    last_price_change_amount
    last_price_change_date
    property_history {
        date
        event_name
        price
        price_change
        price_sqft
        source_name
        source_listing_id
        status
    }
    buyers {
        type
        name
        email
        fulfillment_id
        nrds_id
        mls_set
        state_license
        %s
        office { name email fulfillment_id mls_set %s }
        broker { name fulfillment_id }
    }
""" % (_ADVERTISER_PHONE_FIELDS, _ADVERTISER_PHONE_FIELDS)

#: First Street flood / fire / heat / wind / air risk factors and noise score
DETAIL_ENVIRONMENTAL_RISK_FIELDS = """
    local {
        flood {
            flood_factor_score
            flood_factor_severity
            flood_trend
            fema_zone
            insurance_requirement
            environmental_risk
            firststreet_url
        }
        wildfire {
            fire_factor_score
            fire_factor_severity
            fire_trend
            firststreet_url
        }
        heat {
            heat_factor_score
            heat_factor_severity
            heat_trend
            firststreet_url
        }
        wind {
            wind_factor_score
            wind_factor_severity
            wind_trend
            firststreet_url
        }
        air {
            air_factor_score
            air_factor_severity
            air_trend
            firststreet_url
        }
        noise {
            score
            noise_categories { type text }
        }
    }
"""


def build_property_details_query(property_ids: list[str], selections: list[str]) -> str:
    """Build one GraphQL document that fetches the requested detail selections for many homes.

    Each home is aliased as ``home_<property_id>`` so a single request can cover a batch.
    """
    body = "\n".join(selections)
    homes = "\n".join(
        'home_%s: home(property_id: "%s") { property_id %s }' % (pid, pid, body)
        for pid in property_ids
    )
    return "query GetPropertyDetails { %s }" % homes


#: Realtor.com aggregations used by homeharvest.locations (county / city / postal_code lists)
LOCATION_AGGREGATE_FIELDS = ("county", "city", "postal_code")


def build_location_aggregates_query(fields: list[str], statuses: list[str], size: int) -> str:
    for field in fields:
        if field not in LOCATION_AGGREGATE_FIELDS:
            raise ValueError(f"Unsupported aggregate field {field!r}")
    status_param = f"status: [{', '.join(statuses)}]" if statuses else ""
    return (
        "query GetLocationAggregates($search_location: SearchLocation) { "
        "home_search(query: { search_location: $search_location %s } limit: 1 "
        "aggregate: { field: [%s], size: %d }) { total aggregation } }"
        % (status_param, ", ".join(fields), int(size))
    )
