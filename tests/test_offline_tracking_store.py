"""Offline tests for the tracking store and change detection (phases 2-3)."""

import threading
from datetime import datetime, timedelta, timezone

import pytest

from homeharvest.tracking import ListingStore


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 1, tzinfo=timezone.utc)

    def __call__(self):
        self.t += timedelta(milliseconds=1)  # strictly increasing timestamps
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


@pytest.fixture
def store(tmp_path):
    clock = Clock()
    s = ListingStore(tmp_path / "t.db", clock=clock)
    s.clock = clock
    s.get_or_create_watch("w", {"location": "Lehigh Acres, FL"})
    return s


def row(pid, status="FOR_SALE", price=100_000, sold=None, listing_id=None):
    return {"property_id": pid, "status": status, "list_price": price, "sold_price": sold,
            "listing_id": listing_id or f"L{pid}", "formatted_address": f"{pid} Main St"}


def run(store, chunks: dict[str, list[dict]], complete=True):
    """One run: upsert each chunk, record it, mark removals for complete chunks, finish."""
    r = store.start_run("w")
    out = []
    for chunk, rows in chunks.items():
        out += store.upsert_listings(r["id"], "w", chunk, rows)
        store.record_chunk(r["id"], chunk, "done", len(rows), len(rows), complete=complete)
    store.mark_removed(r["id"], "w", store.complete_chunks(r["id"]), r["started_at"])
    store.finish_run(r["id"], "done")
    store.clock.advance(days=7)
    return {(c["property_id"], c["change_type"]) for c in out} | {
        (c["property_id"], c["change_type"]) for c in store.changes("w", run_id=r["id"])}


def test_first_run_everything_new(store):
    changes = run(store, {"for_sale": [row("1"), row("2")]})
    assert changes == {("1", "new"), ("2", "new")}
    assert len(store.listings("w")) == 2 and len(store.history("1")) == 1


def test_price_changes(store):
    run(store, {"for_sale": [row("1", price=100_000), row("2", price=200_000), row("3", price=300_000)]})
    changes = run(store, {"for_sale": [row("1", price=95_000), row("2", price=210_000), row("3", price=300_000.5)]})
    assert changes == {("1", "price_cut"), ("2", "price_increase")}  # < $1 ignored
    cut = [c for c in store.changes("w", types=["price_cut"])][0]
    assert (cut["amount"], cut["pct"], cut["old_value"], cut["new_value"]) == (-5000, -5.0, "100000", "95000")


def test_status_transitions(store):
    run(store, {"for_sale": [row("1"), row("2"), row("3")], "pending": [row("4", "PENDING")]})
    changes = run(store, {"for_sale": [row("1", "PENDING"), row("4")], "pending": [row("2", "CONTINGENT")],
                          "sold": [row("3", "SOLD", sold=98_000)]})
    assert changes == {("1", "under_contract"), ("2", "under_contract"), ("4", "back_on_market"), ("3", "sold")}
    sold = store.changes("w", types=["sold"])[0]
    assert sold["new_value"] == "98000"


def test_listing_moving_between_chunks_is_not_removed(store):
    run(store, {"for_sale": [row("1")], "sold": []})
    # sold chunk runs after for_sale; the listing left for_sale but shows up as sold
    changes = run(store, {"for_sale": [], "sold": [row("1", "SOLD", sold=90_000)]})
    assert changes == {("1", "sold")}


def test_removed_only_from_complete_chunks(store):
    run(store, {"for_sale": [row("1"), row("2")]})
    assert run(store, {"for_sale": [row("1")]}, complete=False) == set()           # capped/truncated: no removal
    assert run(store, {"for_sale": [row("1")]}) == {("2", "removed")}
    assert [r["property_id"] for r in store.listings("w")] == ["1"]
    assert [r["property_id"] for r in store.listings("w", active=False)] == ["2"]


def test_failed_chunk_does_not_remove(store):
    run(store, {"for_sale": [row("1")], "pending": [row("2", "PENDING")]})
    r = store.start_run("w")
    store.upsert_listings(r["id"], "w", "for_sale", [row("1")])
    store.record_chunk(r["id"], "for_sale", "done", 1, 1, complete=True)
    store.record_chunk(r["id"], "pending", "failed", error="HTTP 503")
    assert store.mark_removed(r["id"], "w", store.complete_chunks(r["id"]), r["started_at"]) == 0


def test_sold_listings_dropping_out_are_not_reported(store):
    run(store, {"sold": [row("1", "SOLD", sold=90_000)]})
    assert run(store, {"sold": []}) == set()
    assert store.listings("w") == []  # deactivated silently


def test_relisted_and_removed_then_sold(store):
    run(store, {"for_sale": [row("1"), row("2"), row("3", listing_id="A")]})
    run(store, {"for_sale": [row("3", listing_id="A")]})                          # 1 and 2 removed
    changes = run(store, {"for_sale": [row("1"), row("3", listing_id="B")], "sold": [row("2", "SOLD", sold=1)]})
    assert changes == {("1", "relisted"), ("3", "relisted"), ("2", "sold")}


def test_watch_params_are_locked(store):
    with pytest.raises(ValueError, match="different search parameters \\(location\\)"):
        store.get_or_create_watch("w", {"location": "Fort Myers, FL"})
    run(store, {"for_sale": [row("1")]})
    store.get_or_create_watch("w", {"location": "Fort Myers, FL"}, reset=True)
    assert store.listings("w") == [] and store.changes("w") == []


def test_unfinished_run_detection(store):
    r = store.start_run("w")
    assert store.unfinished_run("w")["id"] == r["id"]
    store.clock.advance(days=7)
    assert store.unfinished_run("w") is None          # too old to resume
    r2 = store.start_run("w")
    store.finish_run(r2["id"], "done")
    assert store.unfinished_run("w") is None


def test_concurrent_writes(store):
    r = store.start_run("w")

    def write(offset):
        store.upsert_listings(r["id"], "w", f"c{offset}", [row(str(offset * 1000 + i)) for i in range(50)])

    threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store.listings("w")) == 200 and len(store.changes("w")) == 200
