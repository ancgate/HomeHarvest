"""Offline tests for the polite HTTP client (rate limit, retries, block and outage handling)."""

import pytest
import requests

from homeharvest import scrape_property
from homeharvest.core.client import PoliteClient, RateLimiter
from homeharvest.core.scrapers import ScraperInput
from homeharvest.core.scrapers.realtor import RealtorScraper
from homeharvest.exceptions import AuthenticationError, BlockedError, NetworkError, RequestFailedError

URL = "https://www.realtor.com/frontdoor/graphql"


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeResponse:
    def __init__(self, status=200, body=None, text=None, headers=None):
        self.status_code = status
        self._body = body
        self._text = text
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class ScriptedSession:
    """Each post() pops the next scripted outcome (a FakeResponse or an exception)."""

    created = 0

    def __init__(self, script, log):
        self.script, self.log = script, log
        ScriptedSession.created += 1

    def post(self, url, **kwargs):
        self.log.append(kwargs)
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self):
        pass


def make_client(script, clock=None, **kwargs):
    clock = clock or FakeClock()
    log = []
    ScriptedSession.created = 0
    client = PoliteClient(
        base_url=URL, limiter=RateLimiter(clock=clock.clock, sleep=clock.sleep),
        session_factory=lambda: ScriptedSession(script, log), sleep=clock.sleep, clock=clock.clock, **kwargs)
    return client, clock, log


def test_rate_limiter_spaces_requests():
    clock = FakeClock()
    limiter = RateLimiter(jitter=0.0, clock=clock.clock, sleep=clock.sleep)
    for _ in range(5):
        limiter.wait(2.0)
    assert clock.now == pytest.approx(2.0)  # 5 requests at 2/s: first at t=0, last at t=2
    assert limiter.wait(None) == 0 and limiter.wait(0) == 0


def test_rate_limit_jitter_bounds():
    clock = FakeClock()
    limiter = RateLimiter(jitter=0.2, clock=clock.clock, sleep=clock.sleep)
    for _ in range(50):
        limiter.wait(1.0)
    gaps = clock.sleeps
    assert all(0.8 - 1e-9 <= g <= 1.2 + 1e-9 for g in gaps)


def test_success_returns_json_and_sets_timeout():
    client, _, log = make_client([FakeResponse(200, {"data": 1})])
    assert client.post_json(URL, "{}") == {"data": 1}
    assert log[0]["timeout"] == 30.0


def test_retries_5xx_429_and_bot_challenge_with_fresh_sessions():
    script = [FakeResponse(503), FakeResponse(429, headers={"Retry-After": "20"}),
              FakeResponse(200, text="<html>challenge</html>"), FakeResponse(200, {"ok": True})]
    client, clock, _ = make_client(script, requests_per_second=None)
    assert client.post_json(URL, "{}") == {"ok": True}
    assert ScriptedSession.created == 4  # a new session after each failure
    backoffs = clock.sleeps
    assert 1.6 <= backoffs[0] <= 2.4          # 2s ± 20%
    assert backoffs[1] >= 20                   # Retry-After honored
    assert 14.4 <= backoffs[2] <= 21.6         # 18s ± 20%


def test_gives_up_after_max_retries():
    client, _, _ = make_client([FakeResponse(500)] * 3, requests_per_second=None, max_retries=3)
    with pytest.raises(RequestFailedError, match="after 3 attempts"):
        client.post_json(URL, "{}")


def test_403_raises_authentication_error_after_retries():
    client, _, _ = make_client([FakeResponse(403)] * 2, requests_per_second=None, max_retries=2)
    with pytest.raises(AuthenticationError, match="403"):
        client.post_json(URL, "{}")


def test_graphql_400_json_is_returned_not_retried():
    body = {"errors": [{"message": "Required parameter is missing"}]}
    client, clock, _ = make_client([FakeResponse(400, body)], requests_per_second=None)
    assert client.post_json(URL, "{}") == body and clock.sleeps == []


def test_non_json_404_raises_immediately():
    client, clock, _ = make_client([FakeResponse(404)], requests_per_second=None)
    with pytest.raises(RequestFailedError, match="HTTP 404"):
        client.post_json(URL, "{}")
    assert clock.sleeps == []


def test_block_detection_after_consecutive_failures():
    client, _, _ = make_client([FakeResponse(503)] * 20, requests_per_second=None, max_retries=3,
                               block_threshold=5)
    with pytest.raises(RequestFailedError):
        client.post_json(URL, "{}")   # failures 1-3
    with pytest.raises(BlockedError, match="5 consecutive failed requests"):
        client.post_json(URL, "{}")   # failures 4-5 -> blocked
    assert issubclass(BlockedError, AuthenticationError)


def test_success_resets_block_counter():
    script = [FakeResponse(503), FakeResponse(200, {"a": 1}), FakeResponse(503), FakeResponse(200, {"b": 2})]
    client, _, _ = make_client(script, requests_per_second=None, block_threshold=2)
    assert client.post_json(URL, "{}") == {"a": 1}
    assert client.post_json(URL, "{}") == {"b": 2}


def test_network_outage_is_waited_out_without_using_attempts():
    script = [requests.ConnectionError("down")] * 5 + [FakeResponse(200, {"ok": 1})]
    client, clock, _ = make_client(script, requests_per_second=None, max_retries=1)
    assert client.post_json(URL, "{}") == {"ok": 1}
    assert len(clock.sleeps) == 5


def test_network_outage_gives_up_after_grace_period():
    client, _, _ = make_client([requests.Timeout("slow")] * 100, requests_per_second=None,
                               network_grace_seconds=60, network_wait_seconds=15)
    with pytest.raises(NetworkError, match="unreachable for 60s"):
        client.post_json(URL, "{}")


def test_scraper_uses_polite_client_with_settings():
    scraper = RealtorScraper(ScraperInput(location="x", listing_type=None, requests_per_second=0.5, max_retries=2))
    assert scraper.client.requests_per_second == 0.5 and scraper.client.max_retries == 2
    assert scraper.client.timeout == 30.0


def test_scrape_property_validates_politeness_settings():
    with pytest.raises(ValueError, match="requests_per_second"):
        scrape_property("Lehigh Acres, FL", requests_per_second=-1)
    with pytest.raises(ValueError, match="max_retries"):
        scrape_property("Lehigh Acres, FL", max_retries=0)


def test_end_to_end_search_through_the_http_layer(monkeypatch):
    """scrape_property -> _graphql_post -> PoliteClient -> (fake) HTTP session, nothing mocked above HTTP."""
    import json as _json
    from homeharvest.core import client as client_module
    from .fakes import AREA_LOCATION, make_home

    sent = []

    class FakeHTTPSession:
        def post(self, url, data, headers, proxies, timeout):
            payload = _json.loads(data)
            sent.append((url, payload["operationName"], headers.get("Origin"), timeout))
            if payload["operationName"] == "Search_suggestions":
                return FakeResponse(200, {"data": {"search_suggestions": {"geo_results": [AREA_LOCATION]}}})
            if len(sent) == 2:
                return FakeResponse(503)  # one transient failure, retried transparently
            home = make_home("1")
            return FakeResponse(200, {"data": {"homeSearch": {"count": 1, "total": 1, "results": [home]}}})

        def close(self):
            pass

    monkeypatch.setattr(client_module.requests, "Session", FakeHTTPSession)
    monkeypatch.setattr(client_module.time, "sleep", lambda s: None)
    df = scrape_property("Lehigh Acres, FL", listing_type="for_sale", requests_per_second=None)

    assert list(df["property_id"]) == ["1"]
    assert [op for _, op, _, _ in sent] == ["Search_suggestions", "GetHomeSearch", "GetHomeSearch"]
    assert all(url == URL and origin == "https://www.realtor.com" and timeout == 30.0 for url, _, origin, timeout in sent)
