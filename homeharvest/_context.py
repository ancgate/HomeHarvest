"""Internal context shared between scrape_property() and tracking (not public API)."""

from contextvars import ContextVar

#: When set to a dict, scrape_property() copies the scraper's completeness stats into it
#: (location_found, reported_total, capped, truncated_by_limit). Used by homeharvest.tracking.
SEARCH_STATS: ContextVar[dict | None] = ContextVar("homeharvest_search_stats", default=None)
