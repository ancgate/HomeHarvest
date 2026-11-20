"""
homeharvest.tracking
~~~~~~~~~~~~~~~~~~~~

Track listings over time: run the same search ("watch") repeatedly and get what changed since the
last run - new listings, price cuts and increases, under contract, back on the market, sold,
relisted and removed. Everything is kept in one SQLite file; interrupted runs resume.

    >>> from homeharvest import track
    >>> result = track("lehigh-land", location="Lehigh Acres, FL", property_type=["land"])
    >>> result.summary
"""

from .readers import export_changes, get_changes, get_history, get_listings, get_runs, map_changes
from .run import TrackResult, track
from .store import CHANGE_TYPES, ListingStore

__all__ = ["CHANGE_TYPES", "ListingStore", "TrackResult", "track", "get_changes", "get_history",
           "get_listings", "get_runs", "export_changes", "map_changes"]
