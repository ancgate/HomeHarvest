"""
Command line for tracked searches.

    python -m homeharvest.tracking lehigh-land --location "Lehigh Acres, FL" \\
        --listing-type for_sale pending sold --property-type land --export changes.xlsx

Exit code 0 when the run finished (done or partial), 1 when it stopped early (blocked, offline,
interrupted) - run it again to resume - and 2 for bad arguments.
"""

from __future__ import annotations

import argparse
import signal
import sys


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m homeharvest.tracking",
                                description="Track a Realtor.com search over time and report what changed.")
    p.add_argument("name", help="Name of this watch (its search settings are saved under this name)")
    p.add_argument("--location", required=True, help='e.g. "Lehigh Acres, FL", "33971", "Lee County, FL"')
    p.add_argument("--db", default="homeharvest.db", help="SQLite file that keeps the history (default: %(default)s)")
    p.add_argument("--listing-type", nargs="+", help="for_sale for_rent pending sold off_market (default: first four)")
    p.add_argument("--property-type", nargs="+", help="single_family land farm condos townhomes multi_family ...")
    p.add_argument("--split-by", choices=["zip_code"], help="One chunk per ZIP code (big areas, finer resume)")
    for name in ("price", "beds", "baths", "sqft", "lot-sqft", "year-built"):
        kind = float if name == "baths" else int
        p.add_argument(f"--{name}-min", type=kind)
        p.add_argument(f"--{name}-max", type=kind)
    p.add_argument("--past-days", type=int, help="Only listings listed/sold in the last N days")
    p.add_argument("--features", nargs="+", help="Text features, e.g. cul_de_sac corner_lot")
    p.add_argument("--nearby", nargs="+", help="Land categories, e.g. lakefront hunting")
    p.add_argument("--keywords", nargs="+", help="Free-text keywords")
    p.add_argument("--near", nargs="+", metavar="ASPECT[:DIST]", help="Real distance, e.g. cul_de_sac:300ft")
    p.add_argument("--far-from", nargs="+", metavar="ASPECT[:DIST]", help="e.g. power_line:500ft")
    p.add_argument("--history", action="store_true", help="include_property_history for new/changed listings")
    p.add_argument("--risk", action="store_true", help="include_environmental_risk for new/changed listings")
    p.add_argument("--insurance", action="store_true", help="include_insurance for new/changed listings")
    p.add_argument("--market", action="store_true", help="include_market_stats for new/changed listings")
    p.add_argument("--proxy", help="http://user:pass@host:port")
    p.add_argument("--rate", type=float, help="Requests per second (default 2.0)")
    p.add_argument("--export", metavar="PATH", help="Write this run's changes to .csv or .xlsx")
    p.add_argument("--map", metavar="PATH", help="Map this run's changes (.html, .kml, .geojson)")
    p.add_argument("--new-run", action="store_true", help="Start a new run instead of resuming an unfinished one")
    p.add_argument("--reset", action="store_true", help="Forget this watch's history and start over")
    return p


def _proximity(values):
    """['cul_de_sac:300ft', 'water'] -> {'cul_de_sac': '300ft', 'water': None} (None = default radius)."""
    if not values:
        return None
    spec = {}
    for value in values:
        aspect, _, distance = value.partition(":")
        spec[aspect] = distance.strip() or None
    return spec


def build_kwargs(args) -> dict:
    kwargs = {"listing_type": args.listing_type, "property_type": args.property_type, "past_days": args.past_days,
              "features": args.features, "nearby": args.nearby, "keywords": args.keywords, "proxy": args.proxy,
              "near": _proximity(args.near), "far_from": _proximity(args.far_from),
              "include_property_history": args.history, "include_environmental_risk": args.risk,
              "include_insurance": args.insurance, "include_market_stats": args.market}
    if args.rate is not None:
        kwargs["requests_per_second"] = args.rate
    for name in ("price", "beds", "baths", "sqft", "lot_sqft", "year_built"):
        for bound in ("min", "max"):
            kwargs[f"{name}_{bound}"] = getattr(args, f"{name}_{bound}")
    return {k: v for k, v in kwargs.items() if v not in (None, False, [])}


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    from .readers import export_changes, map_changes
    from .run import track

    def _on_term(*_):  # shutdown/logout: stop cleanly so the next run resumes
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, _on_term)
    except ValueError:  # not in the main thread
        pass

    try:
        result = track(args.name, args.location, db=args.db, split_by=args.split_by, resume=not args.new_run,
                       reset=args.reset, **build_kwargs(args))
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    print(f"{result.watch}: run #{result.run_id} {result.status}{' (resumed)' if result.resumed else ''}, "
          f"{len(result.listings)} active listings")
    for kind, count in result.summary.items() or [("changes", 0)]:
        print(f"  {kind:<16}{count:>6}")
    if not result.finished:
        print("Stopped early; run the same command again to resume.")
    if args.export:
        print(f"Changes written to {export_changes(args.db, args.name, args.export, run_id=result.run_id)}")
    if args.map:
        print(f"Map written to {map_changes(args.db, args.name, args.map, run_id=result.run_id)}")
    return 0 if result.finished else 1


if __name__ == "__main__":
    sys.exit(main())
