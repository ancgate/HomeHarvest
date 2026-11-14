"""
homeharvest.features
~~~~~~~~~~~~~~~~~~~~

Keyword and feature matching for property listings.

Realtor.com does not expose filters such as "on a cul-de-sac" or "lakefront", but the
information is usually present in a listing's tags, feature details or description. This
module matches listings against a catalog of named features (``cul_de_sac``, ``lakefront``,
``hunting``, ...) or free-text keywords, and records *why* each one matched.

    >>> from homeharvest import scrape_property
    >>> scrape_property("Lehigh Acres, FL", property_type=["land"], features=["cul_de_sac"])

Matching is tolerant of punctuation and spacing ("cul de sac" matches "Cul-De-Sac" and
"culdesac") and ignores simple negations ("no pool" does not count as a pool).
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

__all__ = [
    "FeatureDefinition",
    "FEATURES",
    "MatchResult",
    "list_features",
    "match_property",
    "filter_properties",
    "validate_feature_keys",
]


# ---------------------------------------------------------------------------
# Listing view: the matchable parts of a listing, regardless of its shape
# ---------------------------------------------------------------------------

#: Street suffixes (and abbreviations) mapped to a canonical form
_STREET_SUFFIXES = {
    "ct": "ct", "court": "ct", "crt": "ct",
    "cir": "cir", "circle": "cir", "circ": "cir",
    "cv": "cv", "cove": "cv",
    "pl": "pl", "place": "pl",
    "way": "way", "ln": "ln", "lane": "ln", "loop": "loop", "lp": "loop",
    "st": "st", "street": "st", "ave": "ave", "avenue": "ave", "rd": "rd", "road": "rd",
    "dr": "dr", "drive": "dr", "blvd": "blvd", "boulevard": "blvd", "ter": "ter", "terrace": "ter",
    "trl": "trl", "trail": "trl", "pkwy": "pkwy", "parkway": "pkwy", "hwy": "hwy", "highway": "hwy",
}


@dataclass
class ListingView:
    text: str = ""
    tags: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)
    street: str = ""
    street_suffix: str | None = None
    style: str | None = None
    beds: float | None = None
    sqft: float | None = None
    lot_sqft: float | None = None
    stories: float | None = None
    new_construction: bool | None = None


def _clean_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number  # NaN check


def _clean_str(value: Any) -> str:
    if value is None:
        return ""
    try:
        if value != value:  # NaN / pd.NA comparisons
            return ""
    except (TypeError, ValueError):
        return ""  # pd.NA raises on bool()
    return str(value)


def _suffix_from_street(street: str) -> str | None:
    """Find the street suffix in a street line like '123 Oak Ct W' -> 'ct'."""
    tokens = [t for t in re.split(r"[\s,.#]+", street.lower()) if t]
    for token in reversed(tokens[1:]):
        if token in _STREET_SUFFIXES:
            return _STREET_SUFFIXES[token]
    return None


def _style_value(style: Any) -> str | None:
    if style is None:
        return None
    value = getattr(style, "value", style)
    value = _clean_str(value)
    return value.upper() or None


def _details_strings(details: Any) -> list[str]:
    out = []
    for item in details or []:
        if isinstance(item, dict):
            category, texts = item.get("category"), item.get("text")
        else:
            category, texts = getattr(item, "category", None), getattr(item, "text", None)
        texts = texts if isinstance(texts, list) else [texts] if texts else []
        joined = "; ".join(_clean_str(t) for t in texts if t)
        if joined:
            out.append(f"{category}: {joined}" if category else joined)
    return out


def _split_tags(tags: Any) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        return [t.strip() for t in tags.split(",") if t.strip()]
    try:
        return [_clean_str(t) for t in tags if _clean_str(t)]
    except TypeError:
        return []


def build_listing_view(listing: Any) -> ListingView:
    """Build a ListingView from a Property model, a raw GraphQL dict or a DataFrame row/dict."""
    # Pydantic Property
    if hasattr(listing, "model_dump") and hasattr(listing, "property_id"):
        description = listing.description
        address = listing.address
        street = (address.street or address.full_line or "") if address else ""
        return ListingView(
            text=_clean_str(description.text) if description else "",
            tags=_split_tags(listing.tags),
            details=_details_strings(listing.details),
            street=street,
            street_suffix=_STREET_SUFFIXES.get((address.street_suffix or "").lower()) if address and address.street_suffix
            else _suffix_from_street(street),
            style=_style_value(description.style or description.type) if description else None,
            beds=_clean_number(description.beds) if description else None,
            sqft=_clean_number(description.sqft) if description else None,
            lot_sqft=_clean_number(description.lot_sqft) if description else None,
            stories=_clean_number(description.stories) if description else None,
            new_construction=listing.new_construction,
        )

    if not hasattr(listing, "get"):
        raise TypeError(f"Unsupported listing type: {type(listing).__name__}")

    # Raw GraphQL dict (has nested location/description)
    if isinstance(listing.get("location"), dict) or isinstance(listing.get("description"), dict):
        description = listing.get("description") or {}
        address = ((listing.get("location") or {}).get("address")) or {}
        street = _clean_str(address.get("line"))
        suffix = _clean_str(address.get("street_suffix")).lower()
        return ListingView(
            text=_clean_str(description.get("text")),
            tags=_split_tags(listing.get("tags")),
            details=_details_strings(listing.get("details")),
            street=street,
            street_suffix=_STREET_SUFFIXES.get(suffix) if suffix else _suffix_from_street(street),
            style=_style_value(description.get("type")),
            beds=_clean_number(description.get("beds")),
            sqft=_clean_number(description.get("sqft")),
            lot_sqft=_clean_number(description.get("lot_sqft")),
            stories=_clean_number(description.get("stories")),
            new_construction=(listing.get("flags") or {}).get("is_new_construction"),
        )

    # Flattened DataFrame row / dict
    street = _clean_str(listing.get("street")) or _clean_str(listing.get("full_street_line"))
    new_construction = listing.get("new_construction")
    return ListingView(
        text=_clean_str(listing.get("text")),
        tags=_split_tags(listing.get("tags")),
        details=[],
        street=street,
        street_suffix=_suffix_from_street(street),
        style=_style_value(listing.get("style")),
        beds=_clean_number(listing.get("beds")),
        sqft=_clean_number(listing.get("sqft")),
        lot_sqft=_clean_number(listing.get("lot_sqft")),
        stories=_clean_number(listing.get("stories")),
        new_construction=new_construction if isinstance(new_construction, bool) else None,
    )


# ---------------------------------------------------------------------------
# Phrase matching
# ---------------------------------------------------------------------------

_SEP = r"[\W_]*"
#: "no pool", "not near a golf course", "isn't gated" - up to two words, stopping at punctuation
_NEGATION = re.compile(r"(?:\b(?:no|not|without|non|never)|n't)(?:[ \t-]+[a-z0-9']+){0,2}[ \t-]*$", re.IGNORECASE)


def _phrase_regex(phrase: str) -> re.Pattern:
    """Compile a phrase so punctuation/spacing between words is optional and plurals match."""
    words = [w for w in re.split(r"[\W_]+", phrase.lower()) if w]
    if not words:
        raise ValueError(f"Empty phrase: {phrase!r}")
    body = _SEP.join(re.escape(w) for w in words)
    return re.compile(rf"(?<![a-z0-9]){body}(?:s|es)?(?![a-z0-9])", re.IGNORECASE)


def _keyword_regex(keyword: str) -> re.Pattern:
    """Like _phrase_regex, but a single compound word (>= 5 letters, e.g. "culdesac", "seawall")
    also matches when the listing splits it up ("cul-de-sac", "sea wall")."""
    words = [w for w in re.split(r"[\W_]+", keyword.lower()) if w]
    if len(words) == 1 and len(words[0]) >= 5:
        body = _SEP.join(re.escape(c) for c in words[0])
        return re.compile(rf"(?<![a-z0-9]){body}(?:s|es)?(?![a-z0-9])", re.IGNORECASE)
    return _phrase_regex(keyword)


def _snippet(text: str, start: int, end: int, width: int = 35) -> str:
    left = max(0, start - width)
    right = min(len(text), end + width)
    snippet = " ".join(text[left:right].split())
    return f"{'…' if left > 0 else ''}{snippet}{'…' if right < len(text) else ''}"


def _find_phrase(regexes: Iterable[re.Pattern], text: str) -> tuple[int, int] | None:
    """First non-negated match of any regex in text."""
    best = None
    for regex in regexes:
        for m in regex.finditer(text):
            if _NEGATION.search(text[max(0, m.start() - 30):m.start()]):
                continue
            if best is None or m.start() < best[0]:
                best = (m.start(), m.end())
            break
    return best


def _normalize_tag(tag: str) -> str:
    return re.sub(r"[\W_]+", "_", tag.strip().lower()).strip("_")


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FeatureDefinition:
    """A named, matchable listing feature.

    :param phrases: Phrases searched in the description and feature details.
    :param tags: Realtor.com listing tags (e.g. ``cul_de_sac``) that count as a match.
    :param street_suffixes: Street suffixes (canonical, e.g. ``ct``) that count as a match.
    :param styles: Property styles (e.g. ``FARM``) that count as a match.
    :param includes: Other feature keys whose matchers also count (composite features).
    :param predicate: Callable(ListingView) -> evidence string or None, for structural checks.
    """

    key: str
    label: str
    group: str
    description: str
    phrases: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    street_suffixes: tuple[str, ...] = ()
    styles: tuple[str, ...] = ()
    includes: tuple[str, ...] = ()
    predicate: Callable[[ListingView], str | None] | None = None
    exclude_phrases: tuple[str, ...] = ()


FEATURES: dict[str, FeatureDefinition] = {}
_COMPILED: dict[str, tuple[re.Pattern, ...]] = {}
_EXCLUDES: dict[str, tuple[re.Pattern, ...]] = {}


def register_feature(definition: FeatureDefinition) -> FeatureDefinition:
    """Add (or replace) a feature in the catalog. Useful for custom, reusable features.

    ``exclude_phrases`` are look-alikes that must not count, e.g. "ranch style" for ranches.
    """
    FEATURES[definition.key] = definition
    _COMPILED[definition.key] = tuple(_phrase_regex(p) for p in definition.phrases)
    _EXCLUDES[definition.key] = tuple(_phrase_regex(p) for p in definition.exclude_phrases)
    return definition


def _new_construction(view: ListingView) -> str | None:
    return "flagged as new construction" if view.new_construction else None


def _single_story(view: ListingView) -> str | None:
    return "1 story" if view.stories == 1 else None


for _definition in (
    # --- street / lot position -------------------------------------------------
    FeatureDefinition(
        "cul_de_sac", "Cul-de-sac", "feature",
        "Listing says the property is on (or at the end of) a cul-de-sac.",
        phrases=("cul de sac", "cul du sac", "culdesac"),
        tags=("cul_de_sac",),
    ),
    FeatureDefinition(
        "likely_cul_de_sac", "Likely cul-de-sac", "feature",
        "Cul-de-sac listings plus streets whose suffix usually means a cul-de-sac (Ct, Court, Cir, Cove). "
        "Broader but less precise than cul_de_sac.",
        street_suffixes=("ct", "cir", "cv"),
        includes=("cul_de_sac",),
    ),
    FeatureDefinition(
        "dead_end", "Dead-end street", "feature", "Dead-end or no-through-traffic street.",
        phrases=("dead end", "dead end street", "no thru traffic", "no through traffic", "end of the road"),
    ),
    FeatureDefinition(
        "corner_lot", "Corner lot", "feature", "Corner lot or parcel.",
        phrases=("corner lot", "corner parcel", "corner homesite", "corner property"),
        tags=("corner_lot",),
    ),
    FeatureDefinition(
        "quiet_street", "Quiet street", "feature", "Quiet / low-traffic street.",
        phrases=("quiet street", "quiet road", "quiet area", "low traffic", "quiet neighborhood"),
    ),
    FeatureDefinition(
        "oversized_lot", "Oversized lot", "feature", "Oversized, double or large lot.",
        phrases=("oversized lot", "double lot", "large lot", "huge lot", "big lot", "extra large lot"),
        tags=("big_yard", "big_lot"),
    ),
    FeatureDefinition(
        "paved_road", "Paved road", "feature", "Lot on a paved road.",
        phrases=("paved road", "paved street", "paved roads"),
    ),
    FeatureDefinition(
        "cleared_lot", "Cleared lot", "feature", "Lot is cleared / ready to build.",
        phrases=("cleared lot", "cleared land", "lot is cleared", "partially cleared", "already cleared"),
    ),
    # --- utilities ---------------------------------------------------------------
    FeatureDefinition(
        "city_water_sewer", "City water / sewer", "feature", "Municipal water and/or sewer available.",
        phrases=("city water", "city sewer", "municipal water", "public water", "public sewer", "county water"),
    ),
    FeatureDefinition(
        "well_septic", "Well & septic", "feature", "Private well and/or septic system.",
        phrases=("well and septic", "septic", "well water", "private well"),
    ),
    FeatureDefinition(
        "utilities_available", "Utilities available", "feature", "Power/utilities at or near the lot.",
        phrases=("utilities available", "electric available", "power available", "utilities at the street",
                 "utilities nearby", "electricity available"),
    ),
    # --- amenities ---------------------------------------------------------------
    FeatureDefinition(
        "pool", "Pool", "feature", "Swimming pool.",
        phrases=("pool", "swimming pool"),
        tags=("swimming_pool", "private_pool", "pool"),
    ),
    FeatureDefinition(
        "golf_course", "Golf course", "feature", "On or near a golf course.",
        phrases=("golf course", "golf"),
        tags=("golf_course", "golf_course_lot_or_frontage", "golf_course_view"),
    ),
    FeatureDefinition(
        "gated_community", "Gated community", "feature", "Gated community.",
        phrases=("gated community", "gated"),
        tags=("gated_community",),
    ),
    FeatureDefinition(
        "view", "View", "feature", "Water, mountain, city or other view.",
        phrases=("water view", "lake view", "ocean view", "river view", "mountain view", "city view",
                 "panoramic view", "golf course view"),
        tags=("view", "water_view", "lake_view", "ocean_view", "river_view", "hill_or_mountain_view", "city_view"),
    ),
    FeatureDefinition(
        "canal_front", "Canal front / gulf access", "feature", "On a canal, with or without gulf access.",
        phrases=("canal front", "canal frontage", "on the canal", "on a canal", "gulf access", "sailboat access",
                 "direct access to the gulf", "freshwater canal", "saltwater canal"),
    ),
    FeatureDefinition(
        "garage", "Garage", "feature", "Garage or carport.",
        phrases=("garage", "carport"),
        tags=("garage_1_or_more", "garage_2_or_more", "garage_3_or_more", "carport"),
    ),
    FeatureDefinition(
        "rv_boat_parking", "RV / boat parking", "feature", "Room or parking for an RV or boat.",
        phrases=("rv parking", "boat parking", "rv and boat parking", "rv boat parking", "room for an rv",
                 "room for your boat", "rv pad"),
        tags=("rv_or_boat_parking",),
    ),
    FeatureDefinition(
        "fenced_yard", "Fenced yard", "feature", "Fenced or partially fenced yard.",
        phrases=("fenced yard", "fenced backyard", "fenced back yard", "fully fenced", "privacy fence"),
        tags=("fenced_yard", "fenced_courtyard"),
    ),
    FeatureDefinition(
        "no_hoa", "No HOA", "feature", "No homeowners association / HOA fees.",
        phrases=("no hoa", "no hoa fees", "no association", "no deed restrictions"),
    ),
    FeatureDefinition(
        "single_story", "Single story", "feature", "Single-story / one-level living.",
        phrases=("single story", "one story", "single level", "one level"),
        tags=("single_story",),
        predicate=_single_story,
    ),
    FeatureDefinition(
        "basement", "Basement", "feature", "Basement.",
        phrases=("basement",),
        tags=("basement", "finished_basement"),
    ),
    FeatureDefinition(
        "fireplace", "Fireplace", "feature", "Fireplace.",
        phrases=("fireplace",),
        tags=("fireplace",),
    ),
    FeatureDefinition(
        "solar", "Solar", "feature", "Solar panels / solar system.",
        phrases=("solar panels", "solar system", "solar power", "owned solar"),
        tags=("solar_panels", "solar_system"),
    ),
    FeatureDefinition(
        "new_construction", "New construction", "feature", "New construction.",
        phrases=("new construction", "newly built", "brand new home"),
        tags=("new_construction",),
        predicate=_new_construction,
    ),
):
    register_feature(_definition)


# --- "nearby" / land categories --------------------------------------------------

_RESIDENTIAL_STYLES = {
    "SINGLE_FAMILY", "MULTI_FAMILY", "CONDO", "CONDOS", "CONDOP", "COOP", "TOWNHOMES", "CONDO_TOWNHOME",
    "CONDO_TOWNHOME_ROWHOME_COOP", "DUPLEX_TRIPLEX", "MOBILE", "APARTMENT",
}


def _has_structure(view: ListingView) -> bool:
    return bool((view.beds or 0) > 0 or (view.sqft or 0) > 0 or (view.style in _RESIDENTIAL_STYLES))


def _house(view: ListingView) -> str | None:
    if not _has_structure(view):
        return None
    facts = [f"{int(view.beds)} beds" if view.beds else None, f"{int(view.sqft):,} sqft" if view.sqft else None]
    facts = [f for f in facts if f]
    return f"has a home ({', '.join(facts)})" if facts else f"has a home ({view.style})"


def _undeveloped(view: ListingView) -> str | None:
    if view.style in ("LAND", "FARM") and not ((view.beds or 0) > 0 or (view.sqft or 0) > 0):
        return f"{view.style.lower()} listing with no structure"
    return None


for _definition in (
    FeatureDefinition(
        "farms_and_ranches", "Farms & ranches", "nearby",
        "Farm, ranch or agricultural land (pasture, crops, livestock, groves).",
        phrases=("farm", "farmland", "ranch", "ranchette", "agricultural", "agriculture", "ag land", "ag exempt",
                 "ag exemption", "agricultural exemption", "pasture", "cattle", "livestock", "crops", "cropland",
                 "orchard", "citrus grove", "orange grove", "pecan grove", "olive grove", "hay field", "barn"),
        tags=("farm", "ranch", "farm_and_ranch"),
        styles=("FARM",),
        exclude_phrases=("ranch style", "ranch home", "ranch house", "ranch floor plan", "ranch layout",
                         "barn door", "farm sink", "farm table", "farm style sink"),
    ),
    FeatureDefinition(
        "hunting", "Hunting land", "nearby", "Hunting land or abundant game (deer, turkey, waterfowl...).",
        phrases=("hunting", "hunt", "hunter", "deer", "turkey", "wild game", "big game", "game animals", "elk",
                 "waterfowl", "duck hunting", "dove field", "quail", "wild hogs", "hog hunting", "food plot",
                 "deer stand", "tree stand", "hunting camp", "hunting lease", "wildlife management area"),
        exclude_phrases=("deer park", "deer creek", "deer run", "turkey creek", "hunters creek", "hunters ridge",
                         "fox hunt", "treasure hunt", "house hunt", "home hunt", "turkey trot"),
    ),
    FeatureDefinition(
        "timberland", "Timberland", "nearby", "Timber, woods or forest land.",
        phrases=("timber", "timberland", "merchantable timber", "wooded", "heavily wooded", "woods", "woodland",
                 "forest", "forested", "hardwood timber", "hardwood trees", "mature hardwoods", "pine trees",
                 "planted pines", "pine plantation", "tree farm", "cypress trees", "national forest"),
        exclude_phrases=("forest hills", "forest park", "timber frame", "wood floors", "woods floors"),
    ),
    FeatureDefinition(
        "undeveloped", "Undeveloped land", "nearby", "Vacant / raw / unimproved land (no structure).",
        phrases=("undeveloped", "vacant land", "vacant lot", "vacant acreage", "raw land", "unimproved",
                 "empty lot", "bare land", "land only", "lot only"),
        predicate=_undeveloped,
    ),
    FeatureDefinition(
        "recreational", "Recreational property", "nearby",
        "Recreational land: camping, RV, ATV, fishing, hiking, cabins, getaways.",
        phrases=("recreational", "recreation", "camping", "campsite", "rv", "atv", "off road", "fishing",
                 "hiking", "hiking trails", "getaway", "weekend getaway", "retreat", "cabin", "off grid", "boating",
                 "kayak", "kayaking", "canoe", "nature preserve", "state park", "national forest"),
        exclude_phrases=("recreation room", "rec room", "rv parking", "corporate retreat"),
    ),
    FeatureDefinition(
        "commercial", "Commercial property", "nearby", "Commercial / mixed-use / industrial property.",
        phrases=("commercial", "zoned commercial", "commercial zoning", "mixed use", "industrial", "warehouse",
                 "office building", "office space", "retail space", "retail center", "retail building",
                 "zoned retail", "business opportunity", "income producing", "highway frontage", "strip center",
                 "c 1 zoning", "c 2 zoning", "zoned c 1", "zoned c 2", "cpd zoning"),
        styles=("COMMERCIAL", "INDUSTRIAL"),
        exclude_phrases=("commercial grade", "commercial appliances", "commercial kitchen appliances",
                         "industrial style", "industrial chic", "close to commercial", "near commercial"),
    ),
    FeatureDefinition(
        "horse", "Horse property", "nearby", "Horse / equestrian property.",
        phrases=("horse", "equestrian", "equine", "horse property", "horse barn", "horse stalls", "paddock",
                 "riding arena", "horse arena", "round pen", "stables", "horse farm", "bridle path", "horse trails"),
        tags=("horse_facilities", "horse_property"),
        exclude_phrases=("horse shoe", "rocking horse", "horsepower"),
    ),
    FeatureDefinition(
        "homesite", "Homesite", "nearby", "Buildable lot / homesite ready for a home.",
        phrases=("homesite", "home site", "building site", "building lot", "buildable lot", "buildable",
                 "build your dream home", "build your dream", "build your home", "build your house",
                 "ready to build", "residential lot", "single family lot", "perfect lot to build",
                 "approved for building", "permit ready"),
    ),
    FeatureDefinition(
        "house", "House", "nearby", "Property includes a home (has beds/sqft or a residential style).",
        phrases=("existing home", "existing house", "includes a home", "includes a house", "home included",
                 "mobile home", "manufactured home"),
        predicate=_house,
    ),
    FeatureDefinition(
        "lakefront", "Lakefront", "nearby", "On or with access to a lake.",
        phrases=("lakefront", "lake front", "lakeside", "on the lake", "lake frontage", "lake access",
                 "private lake", "waterfront lake", "lakefront property"),
        tags=("lakefront", "lake_front", "lake_access"),
    ),
    FeatureDefinition(
        "riverfront", "Riverfront", "nearby", "On or with access to a river or creek.",
        phrases=("riverfront", "river front", "on the river", "river frontage", "river access", "creekfront",
                 "creek front", "creek frontage", "on the creek", "creek runs through", "stream frontage",
                 "streamfront", "bayou frontage"),
        tags=("riverfront", "river_front", "river_access"),
    ),
    FeatureDefinition(
        "oceanfront", "Oceanfront", "nearby", "On or with access to the ocean, gulf, bay or beach.",
        phrases=("oceanfront", "ocean front", "beachfront", "beach front", "gulf front", "gulffront", "bayfront",
                 "bay front", "on the ocean", "on the beach", "ocean access", "beach access", "direct ocean",
                 "sound front", "intracoastal"),
        tags=("oceanfront", "beachfront", "beach_front", "ocean_front"),
    ),
    FeatureDefinition(
        "waterfront", "Waterfront", "nearby",
        "Any waterfront: lakefront, riverfront, oceanfront, canal front, ponds, docks and seawalls.",
        phrases=("waterfront", "water front", "on the water", "water frontage", "seawall", "sea wall", "boat dock",
                 "private dock", "boat lift", "pond front", "pondfront", "on the pond"),
        tags=("waterfront", "water_front"),
        includes=("lakefront", "riverfront", "oceanfront", "canal_front"),
    ),
):
    register_feature(_definition)


def list_features(group: str | None = None) -> list[dict]:
    """Return the feature catalog as a list of dicts (key, label, group, description)."""
    return [
        {"key": f.key, "label": f.label, "group": f.group, "description": f.description}
        for f in FEATURES.values()
        if group is None or f.group == group
    ]


def validate_feature_keys(keys: Iterable[str] | None, param_name: str = "features") -> list[str]:
    """Normalize feature keys and raise a helpful ValueError for unknown ones."""
    if keys is None:
        return []
    if isinstance(keys, str):
        keys = [keys]
    normalized = []
    for key in keys:
        norm = _normalize_tag(str(key))
        if norm not in FEATURES:
            suggestions = difflib.get_close_matches(norm, FEATURES.keys(), n=3)
            hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
            raise ValueError(
                f"Unknown {param_name} value {key!r}.{hint} "
                f"Use homeharvest.list_features() to see all options, or pass free text via keywords=[...]."
            )
        if norm not in normalized:
            normalized.append(norm)
    return normalized


def _normalize_keywords(keywords: Iterable[str] | None) -> list[str]:
    if keywords is None:
        return []
    if isinstance(keywords, str):
        keywords = [keywords]
    out = []
    for keyword in keywords:
        keyword = " ".join(str(keyword).split())
        if keyword and re.search(r"[A-Za-z0-9]", keyword) and keyword not in out:
            out.append(keyword)
    return out


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

@dataclass
class MatchResult:
    features: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    evidence: dict[str, str] = field(default_factory=dict)

    @property
    def matched(self) -> bool:
        return bool(self.features or self.keywords)


def _mask(text: str, excludes: tuple[re.Pattern, ...]) -> str:
    """Blank out excluded look-alike phrases, keeping offsets so snippets still line up."""
    for regex in excludes:
        text = regex.sub(lambda m: " " * len(m.group(0)), text)
    return text


def _match_text_sources(regexes: tuple[re.Pattern, ...], view: ListingView,
                        excludes: tuple[re.Pattern, ...] = ()) -> str | None:
    if not regexes:
        return None
    if view.text and (span := _find_phrase(regexes, _mask(view.text, excludes))):
        return f'description: "{_snippet(view.text, *span)}"'
    for detail in view.details:
        if span := _find_phrase(regexes, _mask(detail, excludes)):
            return f'details: "{_snippet(detail, *span, width=60)}"'
    return None


def _match_feature(key: str, view: ListingView, _seen: set[str] | None = None) -> str | None:
    """Return evidence for why the listing has this feature, or None."""
    seen = _seen or set()
    if key in seen:
        return None
    seen.add(key)
    definition = FEATURES[key]

    if definition.tags:
        wanted = {_normalize_tag(t) for t in definition.tags}
        for tag in view.tags:
            if _normalize_tag(tag) in wanted:
                return f'tag "{tag}"'

    if evidence := _match_text_sources(_COMPILED.get(key, ()), view, _EXCLUDES.get(key, ())):
        return evidence

    if definition.styles and view.style in definition.styles:
        return f"property type {view.style}"

    if definition.predicate and (evidence := definition.predicate(view)):
        return evidence

    for included in definition.includes:
        if evidence := _match_feature(included, view, seen):
            return evidence

    if definition.street_suffixes and view.street_suffix in definition.street_suffixes:
        return f'street "{view.street}" ({view.street_suffix.upper()} suffix)'

    return None


def _match_keyword(keyword: str, view: ListingView) -> str | None:
    regex = (_keyword_regex(keyword),)
    if view.text and (span := _find_phrase(regex, view.text)):
        return f'description: "{_snippet(view.text, *span)}"'
    for detail in view.details:
        if span := _find_phrase(regex, detail):
            return f'details: "{_snippet(detail, *span, width=60)}"'
    for tag in view.tags:
        if _find_phrase(regex, tag.replace("_", " ")):
            return f'tag "{tag}"'
    return None


def match_property(listing: Any, features: Iterable[str] | None = None,
                   keywords: Iterable[str] | None = None) -> MatchResult:
    """Check one listing (Property, raw dict or DataFrame row) against features and keywords."""
    view = listing if isinstance(listing, ListingView) else build_listing_view(listing)
    result = MatchResult()
    for key in validate_feature_keys(features):
        if evidence := _match_feature(key, view):
            result.features.append(key)
            result.evidence[key] = evidence
    for keyword in _normalize_keywords(keywords):
        if evidence := _match_keyword(keyword, view):
            result.keywords.append(keyword)
            result.evidence[keyword] = evidence
    return result


class FeatureMatcher:
    """Validated matcher for a combination of feature groups.

    ``features``, ``nearby`` and ``keywords`` are separate groups that must *all* be satisfied.
    Within each group, ``match_mode="any"`` (default) needs one hit and ``"all"`` needs every term.
    """

    def __init__(self, features=None, nearby=None, keywords=None, match_mode: str = "any"):
        if match_mode not in ("any", "all"):
            raise ValueError(f"match_mode must be 'any' or 'all', got {match_mode!r}")
        self.features = validate_feature_keys(features, "features")
        self.nearby = validate_feature_keys(nearby, "nearby")
        self.keywords = _normalize_keywords(keywords)
        self.match_mode = match_mode

    @property
    def active(self) -> bool:
        return bool(self.features or self.nearby or self.keywords)

    def match(self, listing: Any) -> tuple[bool, MatchResult]:
        all_features = self.features + [k for k in self.nearby if k not in self.features]
        result = match_property(listing, all_features, self.keywords)
        return self._passes(result), result

    def _passes(self, result: MatchResult) -> bool:
        for requested, matched in (
            (self.features, result.features),
            (self.nearby, result.features),
            (self.keywords, result.keywords),
        ):
            if not requested:
                continue
            hits = [term for term in requested if term in matched]
            if self.match_mode == "all" and len(hits) != len(requested):
                return False
            if self.match_mode == "any" and not hits:
                return False
        return True


def annotate_listing(listing: Any, result: MatchResult) -> None:
    """Store a MatchResult on a Property model or raw dict (in place)."""
    values = {
        "matched_features": result.features or None,
        "matched_keywords": result.keywords or None,
        "feature_evidence": result.evidence or None,
    }
    if isinstance(listing, dict):
        listing.update(values)
    else:
        for name, value in values.items():
            setattr(listing, name, value)


def filter_properties(properties: Any, features=None, nearby=None, keywords=None,
                      match_mode: str = "any", only_matching: bool = True):
    """Filter (or just annotate) already-scraped properties.

    Works with a pandas DataFrame from ``scrape_property()``, a list of ``Property`` models or a
    list of raw dicts. Adds ``matched_features``, ``matched_keywords`` and ``feature_evidence``.

    :param only_matching: True (default) drops non-matching listings; False keeps every listing
        and only annotates the matches (handy for highlighting on a map).
    """
    matcher = FeatureMatcher(features, nearby, keywords, match_mode)

    try:
        import pandas as pd
    except ImportError:  # pragma: no cover - pandas is a hard dependency
        pd = None

    if pd is not None and isinstance(properties, pd.DataFrame):
        df = properties.copy()
        if df.empty:
            for column in ("matched_features", "matched_keywords", "feature_evidence"):
                df[column] = pd.Series(dtype="object")
            return df
        matched_features, matched_keywords, evidence, keep = [], [], [], []
        for _, row in df.iterrows():
            passes, result = matcher.match(row.to_dict())
            keep.append(passes)
            matched_features.append(", ".join(result.features) if passes and result.features else pd.NA)
            matched_keywords.append(", ".join(result.keywords) if passes and result.keywords else pd.NA)
            evidence.append(
                "; ".join(f"{k}: {v}" for k, v in result.evidence.items()) if passes and result.evidence else pd.NA
            )
        df["matched_features"] = matched_features
        df["matched_keywords"] = matched_keywords
        df["feature_evidence"] = evidence
        if only_matching:
            df = df[keep].reset_index(drop=True)
        return df

    out = []
    for listing in properties:
        passes, result = matcher.match(listing)
        annotate_listing(listing, result if passes else MatchResult())
        if passes or not only_matching:
            out.append(listing)
    return out
