"""
homeharvest.core.client
~~~~~~~~~~~~~~~~~~~~~~~

Polite HTTP transport: a shared rate limit, retries with backoff, fresh sessions after
failures, block detection and waiting out short network outages.
"""

from __future__ import annotations

import random
import threading
import time
from typing import Callable
from urllib.parse import urlparse

import requests

from ..exceptions import AuthenticationError, BlockedError, NetworkError, RequestFailedError


class RateLimiter:
    """Thread-safe limiter: successive calls are spaced ``1 / rate`` seconds apart (± jitter).

    A caller reserves the next slot under a lock and sleeps outside it, so threads queue up
    instead of bursting.
    """

    def __init__(self, jitter: float = 0.2, clock: Callable[[], float] | None = None,
                 sleep: Callable[[float], None] | None = None):
        self.jitter = jitter
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next = 0.0

    def clock(self) -> float:
        return (self._clock or time.monotonic)()

    def sleep(self, seconds: float) -> None:
        (self._sleep or time.sleep)(seconds)

    def wait(self, rate: float | None) -> float:
        """Block until the caller may send a request at ``rate`` requests/second. Returns the delay."""
        if not rate or rate <= 0:
            return 0.0
        with self._lock:
            now = self.clock()
            start = max(now, self._next)
            interval = (1.0 / rate) * random.uniform(1 - self.jitter, 1 + self.jitter)
            self._next = start + interval
        delay = start - now
        if delay > 0:
            self.sleep(delay)
        return delay


_LIMITERS: dict[str, RateLimiter] = {}
_LIMITERS_LOCK = threading.Lock()


def limiter_for(url: str) -> RateLimiter:
    """The process-wide limiter for a host, shared by every scraper and thread."""
    host = urlparse(url).netloc
    with _LIMITERS_LOCK:
        return _LIMITERS.setdefault(host, RateLimiter())


def _retry_after_seconds(response) -> float | None:
    value = (getattr(response, "headers", None) or {}).get("Retry-After")
    try:
        return max(0.0, float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None  # HTTP-date form: fall back to our own backoff


class PoliteClient:
    """POST JSON payloads politely.

    * waits for the shared per-host rate limit before every request (``requests_per_second``,
      ``None`` or 0 disables it);
    * one ``requests.Session`` per thread, discarded after any failure (fresh connection/cookies);
    * retries HTTP 403, 429 and 5xx and non-JSON 200 answers (bot challenges) up to
      ``max_retries`` attempts with exponential backoff and ±20% jitter, honoring ``Retry-After``;
    * other 4xx answers are returned as JSON if possible (GraphQL errors), else raised at once;
    * raises :class:`BlockedError` after ``block_threshold`` consecutive failures of any kind;
    * waits out network outages (connection errors/timeouts) without using attempts, then raises
      :class:`NetworkError` after ``network_grace_seconds``.
    """

    def __init__(self, *, base_url: str, requests_per_second: float | None = 2.0, max_retries: int = 4,
                 proxies: dict | None = None, headers: dict | None = None, timeout: float = 30.0,
                 block_threshold: int = 10, network_grace_seconds: float = 180.0,
                 network_wait_seconds: float = 15.0, backoff_base: float = 2.0, backoff_factor: float = 3.0,
                 backoff_max: float = 60.0, limiter: RateLimiter | None = None,
                 session_factory: Callable[[], requests.Session] | None = None,
                 sleep: Callable[[float], None] | None = None, clock: Callable[[], float] | None = None):
        if requests_per_second is not None and requests_per_second < 0:
            raise ValueError("requests_per_second must be positive (or None to disable the limit)")
        if max_retries < 1:
            raise ValueError("max_retries must be at least 1")
        self.requests_per_second = requests_per_second
        self.max_retries = max_retries
        self.proxies = proxies
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.block_threshold = block_threshold
        self.network_grace_seconds = network_grace_seconds
        self.network_wait_seconds = network_wait_seconds
        self.backoff_base = backoff_base
        self.backoff_factor = backoff_factor
        self.backoff_max = backoff_max
        self.limiter = limiter or limiter_for(base_url)
        self.session_factory = session_factory or requests.Session
        self._sleep = sleep
        self._clock = clock
        self._local = threading.local()
        self._state_lock = threading.Lock()
        self._consecutive_failures = 0

    def sleep(self, seconds: float) -> None:
        (self._sleep or time.sleep)(seconds)

    def clock(self) -> float:
        return (self._clock or time.monotonic)()

    # -- sessions ----------------------------------------------------------
    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = self.session_factory()
            self._local.session = session
        return session

    def _reset_session(self) -> None:
        session = getattr(self._local, "session", None)
        self._local.session = None
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001 - closing a broken session must never fail the request
                pass

    # -- failure bookkeeping ---------------------------------------------------
    def _record_success(self) -> None:
        with self._state_lock:
            self._consecutive_failures = 0

    def _record_failure(self, reason: str, response) -> None:
        with self._state_lock:
            self._consecutive_failures += 1
            failures = self._consecutive_failures
        if failures >= self.block_threshold:
            raise BlockedError(
                f"{failures} consecutive failed requests (last: {reason}). The site is most likely blocking "
                "requests; wait a while, lower requests_per_second or use a proxy.",
                response=response,
            )

    def _backoff(self, attempt: int, response) -> float:
        delay = min(self.backoff_max, self.backoff_base * self.backoff_factor ** (attempt - 1))
        delay *= random.uniform(0.8, 1.2)
        retry_after = _retry_after_seconds(response)
        if retry_after is not None:
            delay = max(delay, min(retry_after, 300.0))
        return delay

    # -- request -----------------------------------------------------------------
    def post_json(self, url: str, data: str) -> dict:
        attempt = 0
        offline_since = None
        while True:
            self.limiter.wait(self.requests_per_second)
            try:
                response = self._session().post(
                    url, data=data, headers=self.headers, proxies=self.proxies, timeout=self.timeout
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                self._reset_session()
                now = self.clock()
                offline_since = now if offline_since is None else offline_since
                if now - offline_since >= self.network_grace_seconds:
                    raise NetworkError(
                        f"Network unreachable for {self.network_grace_seconds:.0f}s while calling {url}: {exc}"
                    ) from exc
                self.sleep(self.network_wait_seconds * random.uniform(0.8, 1.2))
                continue
            offline_since = None

            status = response.status_code
            if status == 403 or status == 429 or status >= 500:
                reason = f"HTTP {status}"
            else:
                try:
                    payload = response.json()
                except ValueError:
                    if 400 <= status < 500:
                        self._record_success()  # the site answered; this request is just bad
                        raise RequestFailedError(f"HTTP {status} from {url}", response=response)
                    reason = f"HTTP {status} without a JSON body (likely a bot challenge)"
                else:
                    self._record_success()
                    return payload

            self._reset_session()
            self._record_failure(reason, response)  # may raise BlockedError
            attempt += 1
            if attempt >= self.max_retries:
                if status == 403:
                    raise AuthenticationError(
                        f"Received 403 Forbidden from {urlparse(url).netloc} after {attempt} attempts.",
                        response=response,
                    )
                raise RequestFailedError(f"{reason} from {url} after {attempt} attempts", response=response)
            self.sleep(self._backoff(attempt, response))
