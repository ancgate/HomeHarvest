"""Offline end-to-end tests for track() and the readers (phases 4-5)."""

import warnings

import pandas as pd
import pytest

from homeharvest import (export_changes, get_changes, get_history, get_listings, get_runs, map_changes, track)
from homeharvest.core.scrapers.realtor import RealtorScraper
from homeharvest.exceptions import BlockedError

from .fakes import FakeRealtorAPI, make_home

TYPES = ["for_sale", "pending", "sold"]


class Api:
    """FakeRealtorAPI whose homes can change between runs, and that can fail on demand."""

    def __init__(self, monkeypatch, homes):
        self.fake = FakeRealtorAPI(homes=homes, details={})
        self.fail_on = None          # e.g. ("sold", BlockedError(...)) to fail the sold chunk once
        self.detail_ids = []
        monkeypatch.setattr(RealtorScraper, "_graphql_post", lambda s, q, v, op: self(s, q, v, op))

    def __call__(self, scraper, query, variables, op):
        if op == "GetHomeSearch" and self.fail_on and f"status: {self.fail_on[0]}" in query:
            error, self.fail_on = self.fail_on[1], None
            raise error
        if op == "GetPropertyDetails":
            import re
            self.detail_ids += re.findall(r'home_([0-9A-Za-z]+): home\(', query)
        return self.fake(scraper, query, variables, op)

    def set(self, homes):
        self.fake.homes = homes


def week1():
    return [
        make_home("1", list_price=100_000),
        make_home("2", list_price=200_000),
        make_home("3", list_price=300_000),
        make_home("4", list_price=150_000, is_pending=True),          # PENDING
        make_home("5", "sold", sold_days_ago=3, list_price=90_000),
    ]


def week2():
    return [
        make_home("1", list_price=95_000),                            # price cut
        make_home("2", list_price=200_000, is_pending=True),          # under contract
        # 3 is gone                                                    # removed
        make_home("4", list_price=150_000),                           # back on market
        make_home("5", "sold", sold_days_ago=10, list_price=90_000),
        make_home("6", list_price=50_000),                            # new
    ]


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "track.db")


def test_two_runs_report_every_kind_of_change(monkeypatch, db):
    api = Api(monkeypatch, week1())
    first = track("lehigh", location="Lehigh Acres, FL", listing_type=TYPES, db=db)
    assert first.status == "done" and not first.resumed
    assert first.summary == {"new": 5}
    assert set(first.listings["property_id"]) == {"1", "2", "3", "4", "5"}

    api.set(week2())
    second = track("lehigh", location="Lehigh Acres, FL", listing_type=TYPES, db=db)
    got = {(r.property_id, r.change_type) for r in second.changes.itertuples()}
    assert got == {("1", "price_cut"), ("2", "under_contract"), ("3", "removed"), ("4", "back_on_market"),
                   ("6", "new")}
    cut = second.changes.set_index("change_type").loc["price_cut"]
    assert cut["amount"] == -5000 and cut["pct"] == -5.0 and cut["change"].startswith("Price cut: $100,000 -> $95,000")
    assert cut["formatted_address"]  # listing data comes along with the change
    assert "3" not in set(second.listings["property_id"])


def test_interrupted_run_resumes_and_matches_an_uninterrupted_one(monkeypatch, tmp_path):
    clean_db, broken_db = str(tmp_path / "clean.db"), str(tmp_path / "broken.db")
    api = Api(monkeypatch, week1())
    track("w", location="Lehigh Acres, FL", listing_type=TYPES, db=clean_db)
    track("w", location="Lehigh Acres, FL", listing_type=TYPES, db=broken_db)
    api.set(week2())
    clean = track("w", location="Lehigh Acres, FL", listing_type=TYPES, db=clean_db)

    api.fail_on = ("sold", BlockedError("blocked", response=None))
    stopped = track("w", location="Lehigh Acres, FL", listing_type=TYPES, db=broken_db)
    assert stopped.status == "blocked" and not stopped.finished
    assert stopped.chunks == {"for_sale": "done", "pending": "done"}
    assert "removed" not in stopped.summary           # removals wait for the end of the run

    searches_before = len(api.fake.queries("GetHomeSearch"))
    resumed = track("w", location="Lehigh Acres, FL", listing_type=TYPES, db=broken_db)
    assert resumed.resumed and resumed.status == "done" and resumed.run_id == stopped.run_id
    assert len(api.fake.queries("GetHomeSearch")) - searches_before == 1   # only the sold chunk ran again
    assert resumed.summary == clean.summary


def test_new_run_instead_of_resuming(monkeypatch, db):
    api = Api(monkeypatch, week1())
    api.fail_on = ("sold", BlockedError("blocked", response=None))
    stopped = track("w", location="Lehigh Acres, FL", listing_type=TYPES, db=db)
    fresh = track("w", location="Lehigh Acres, FL", listing_type=TYPES, db=db, resume=False)
    assert fresh.run_id != stopped.run_id and not fresh.resumed and fresh.status == "done"


def test_capped_chunk_never_removes(monkeypatch, db):
    monkeypatch.setattr(RealtorScraper, "API_RESULT_CAP", 3)
    homes = [make_home(str(i), "off_market") for i in range(5)]
    api = Api(monkeypatch, homes)
    api.fake.cap = 3
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        track("w", location="Lehigh Acres, FL", listing_type="off_market", db=db)
        api.set(homes[::-1])  # a different 3 come back first
        second = track("w", location="Lehigh Acres, FL", listing_type="off_market", db=db)
    assert "removed" not in second.summary
    assert get_runs(db, "w")["status"].tolist() == ["done", "done"]


def test_failed_chunk_makes_a_partial_run_without_removals(monkeypatch, db):
    api = Api(monkeypatch, week1())
    track("w", location="Lehigh Acres, FL", listing_type=TYPES, db=db)
    api.set(week2())
    api.fail_on = ("for_sale", RuntimeError("HTTP 500"))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = track("w", location="Lehigh Acres, FL", listing_type=TYPES, db=db)
    assert result.status == "partial" and result.chunks["for_sale"] == "failed"
    assert "removed" not in result.summary
    assert any("will be retried" in str(w.message) for w in caught)


def test_location_not_found_is_a_failed_chunk(monkeypatch, db):
    Api(monkeypatch, week1())
    track("w", location="Lehigh Acres, FL", listing_type=["for_sale"], db=db)
    monkeypatch.setattr(RealtorScraper, "handle_location", lambda self: None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = track("w", location="Lehigh Acres, FL", listing_type=["for_sale"], db=db)
    assert result.status == "partial" and result.summary == {}   # nothing "removed"


def test_watch_parameters_are_locked(monkeypatch, db):
    Api(monkeypatch, week1())
    track("w", location="Lehigh Acres, FL", listing_type=TYPES, db=db)
    with pytest.raises(ValueError, match="different search parameters"):
        track("w", location="Lehigh Acres, FL", listing_type=TYPES, price_max=500_000, db=db)
    reset = track("w", location="Lehigh Acres, FL", listing_type=TYPES, price_max=500_000, db=db, reset=True)
    assert reset.summary == {"new": 5}


@pytest.mark.parametrize("bad", [{"limit": 10}, {"offset": 200}, {"return_type": "raw"}])
def test_rejected_arguments(db, bad):
    with pytest.raises(ValueError, match="always collects every matching listing"):
        track("w", location="Lehigh Acres, FL", db=db, **bad)


def test_details_only_for_new_or_changed(monkeypatch, db):
    api = Api(monkeypatch, week1())
    track("w", location="Lehigh Acres, FL", listing_type=["for_sale"], include_insurance=True, db=db)
    assert sorted(set(api.detail_ids)) == ["1", "2", "3", "4"]
    api.detail_ids.clear()
    api.set(week2())
    track("w", location="Lehigh Acres, FL", listing_type=["for_sale"], include_insurance=True, db=db)
    assert sorted(set(api.detail_ids)) == ["1", "2", "4", "6"]   # 3 is gone, nothing unchanged was fetched


def test_split_by_zip_code(monkeypatch, db):
    api = Api(monkeypatch, [
        make_home("1", postal_code="33971"), make_home("2", postal_code="33936"),
        make_home("3", postal_code="33936", city="Fort Myers"),   # same ZIP, other city: filtered out
    ])
    api.fake.aggregation = {"postal_code": [{"key": "33971", "count": 1}, {"key": "33936", "count": 2}]}
    real = api.fake._search_results
    api.fake._search_results = lambda q: [h for h in real(q)
                                          if not (m := __import__("re").search(r'"location":"(\d{5})"', str(api.fake.calls[-1][2])))
                                          or h["location"]["address"]["postal_code"] == m.group(1)]
    result = track("w", location="Lehigh Acres, FL", listing_type=["for_sale"], split_by="zip_code", db=db)
    assert set(result.chunks) == {"for_sale|zip=33971", "for_sale|zip=33936"}
    assert set(result.listings["property_id"]) == {"1", "2"}


def test_split_by_zip_code_falls_back(monkeypatch, db):
    import homeharvest.locations
    Api(monkeypatch, week1())
    monkeypatch.setattr(homeharvest.locations, "get_zip_codes", lambda **kw: [])
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = track("w", location="Lehigh Acres, FL", listing_type=["for_sale"], split_by="zip_code", db=db)
    assert set(result.chunks) == {"for_sale"}
    assert any("not splitting by ZIP" in str(w.message) for w in caught)


# ---------------------------------------------------------------------------
# Readers and exports (phase 5)
# ---------------------------------------------------------------------------

@pytest.fixture
def two_runs(monkeypatch, db):
    api = Api(monkeypatch, week1())
    track("w", location="Lehigh Acres, FL", listing_type=TYPES, db=db)
    api.set(week2())
    track("w", location="Lehigh Acres, FL", listing_type=TYPES, db=db)
    return db


def test_readers(two_runs):
    all_changes = get_changes(two_runs, "w")
    assert len(all_changes) == 10 and list(all_changes.columns[:4]) == ["watch", "run_id", "at", "change_type"]
    cuts = get_changes(two_runs, "w", types="price_cut")
    assert list(cuts["property_id"]) == ["1"]
    assert get_changes(two_runs, "w", since="2999-01-01").empty

    history = get_history(two_runs, "1")
    assert history["list_price"].tolist() == [100_000, 95_000]
    assert set(get_listings(two_runs, "w", active=False)["property_id"]) == {"3"}
    assert get_runs(two_runs, "w")["status"].tolist() == ["done", "done"]
    with pytest.raises(FileNotFoundError):
        get_changes(two_runs + ".missing", "w")


def test_export_csv_and_excel(two_runs, tmp_path):
    csv_path = export_changes(two_runs, "w", tmp_path / "changes.csv")      # latest run by default
    assert len(pd.read_csv(csv_path)) == 5
    xlsx = export_changes(two_runs, "w", tmp_path / "changes.xlsx")
    sheets = pd.read_excel(xlsx, sheet_name=None)
    assert set(sheets) == {"Changes", "Listings", "History", "Runs"} and len(sheets["Changes"]) == 5


def test_map_changes(two_runs, tmp_path):
    import json
    page = open(map_changes(two_runs, "w", tmp_path / "changes.html")).read()
    points = json.loads(page.split("const POINTS = ", 1)[1].split(";\n", 1)[0])
    assert len(points) == 5 and "w: 5 changed listings" in page
    assert any("Price cut: $100,000 -&gt; $95,000" in p["popup"] for p in points)
    assert any(">Removed<" in p["popup"] for p in points)   # removed listings stay on the map at their last location
    highlighted = sum(p["is_match"] for p in points)
    assert highlighted == 3   # price cut, new, back on market


# ---------------------------------------------------------------------------
# Command line (phase 6)
# ---------------------------------------------------------------------------

def test_cli_runs_reports_and_exports(monkeypatch, tmp_path, capsys):
    from homeharvest.tracking.__main__ import main

    db = str(tmp_path / "cli.db")
    argv = ["lehigh", "--location", "Lehigh Acres, FL", "--db", db, "--listing-type", "for_sale", "pending", "sold"]
    api = Api(monkeypatch, week1())
    assert main(argv) == 0
    assert "lehigh: run #1 done, 5 active listings" in capsys.readouterr().out

    api.set(week2())
    assert main(argv + ["--export", str(tmp_path / "c.xlsx"), "--map", str(tmp_path / "c.html")]) == 0
    out = capsys.readouterr().out
    assert "price_cut" in out and "removed" in out and "Changes written to" in out and "Map written to" in out
    assert (tmp_path / "c.xlsx").exists() and (tmp_path / "c.html").exists()


def test_cli_exit_codes(monkeypatch, tmp_path, capsys):
    from homeharvest.tracking.__main__ import main

    db = str(tmp_path / "cli.db")
    api = Api(monkeypatch, week1())
    api.fail_on = ("sold", BlockedError("blocked", response=None))
    argv = ["w", "--location", "Lehigh Acres, FL", "--db", db, "--listing-type", "for_sale", "sold"]
    assert main(argv) == 1 and "run the same command again to resume" in capsys.readouterr().out
    assert main(argv) == 0 and "(resumed)" in capsys.readouterr().out
    assert main(argv + ["--price-max", "5"]) == 2 and "different search parameters" in capsys.readouterr().err


def test_cli_builds_scrape_arguments():
    from homeharvest.tracking.__main__ import _parser, build_kwargs

    args = _parser().parse_args(["w", "--location", "x", "--property-type", "land", "--price-max", "50000",
                                 "--near", "cul_de_sac:300ft", "water", "--risk", "--rate", "1"])
    assert build_kwargs(args) == {"property_type": ["land"], "price_max": 50000,
                                  "near": {"cul_de_sac": "300ft", "water": None},
                                  "include_environmental_risk": True, "requests_per_second": 1.0}


def test_cli_import_does_not_shadow_track_function():
    import homeharvest
    import homeharvest.tracking.__main__  # noqa: F401
    assert callable(homeharvest.track)
