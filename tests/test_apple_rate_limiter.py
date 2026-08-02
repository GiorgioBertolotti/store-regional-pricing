"""apple._RateLimiter coordinates request pacing across all worker threads hitting Apple's
API. A per-request-only retry (each thread independently sleeping and retrying on its own
schedule) turned out not to be enough in practice: with 8 concurrent threads, one thread's
429 usually means the others are seconds away from tripping the same limit too, so
independent retries just re-trigger it repeatedly. The fix is a single shared limiter: a
429 on any thread pushes back the "next allowed request" time for every thread, and the
minimum inter-request interval caps the aggregate rate regardless of worker count."""

import time

import requests

from store_pricing.apple import _RateLimiter, _request_with_rate_limit, _retry_after_seconds


class _FakeResponse:
    def __init__(self, headers=None):
        self.headers = headers or {}


def test_wait_enforces_minimum_interval_between_calls():
    limiter = _RateLimiter(min_interval=0.05)
    start = time.monotonic()
    limiter.wait()
    limiter.wait()
    limiter.wait()
    elapsed = time.monotonic() - start
    # Three calls spaced at least min_interval apart -> at least 2 intervals elapsed.
    assert elapsed >= 0.1


def test_backoff_delays_the_next_wait_call():
    limiter = _RateLimiter(min_interval=0.0)
    limiter.backoff(0.1)
    start = time.monotonic()
    limiter.wait()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.09  # small tolerance for scheduling jitter


def test_backoff_never_moves_the_allowed_time_earlier():
    limiter = _RateLimiter(min_interval=0.0)
    limiter.backoff(1.0)
    first_next_allowed = limiter._next_allowed
    limiter.backoff(0.01)  # a shorter backoff must not shrink the existing cooldown
    assert limiter._next_allowed == first_next_allowed


def test_backoff_is_shared_across_multiple_wait_callers():
    # Simulates one worker thread hitting a 429 and calling backoff() while another
    # thread is about to call wait() - the second thread must also be held back, not
    # just the one that got rate-limited.
    limiter = _RateLimiter(min_interval=0.0)
    limiter.backoff(0.1)

    start = time.monotonic()
    limiter.wait()  # a different "thread" than the one that called backoff()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.09


def test_retry_after_header_is_used_when_present():
    resp = _FakeResponse(headers={"Retry-After": "3"})
    assert _retry_after_seconds(resp, attempt=0) == 3.0


def test_retry_after_falls_back_to_exponential_backoff_when_header_missing():
    resp = _FakeResponse(headers={})
    assert _retry_after_seconds(resp, attempt=0) == 2.0
    assert _retry_after_seconds(resp, attempt=1) == 4.0


def test_retry_after_falls_back_on_unparseable_header():
    resp = _FakeResponse(headers={"Retry-After": "not-a-number"})
    assert _retry_after_seconds(resp, attempt=0) == 2.0


# --- _request_with_rate_limit(): network-level failures must not crash the caller ---
# A ReadTimeout from a worker thread inside a ThreadPoolExecutor previously propagated all
# the way up through executor.map() and crashed the entire run (observed live against
# Apple's API under sustained load) - only HTTP status codes were handled, not exceptions.

def test_persistent_timeout_returns_a_failed_response_instead_of_raising(monkeypatch):
    def _always_times_out(method, url, headers=None, json=None, timeout=None):
        raise requests.exceptions.ReadTimeout("simulated timeout")

    monkeypatch.setattr(requests, "request", _always_times_out)
    monkeypatch.setattr("store_pricing.apple._rate_limiter", _RateLimiter(min_interval=0.0))

    resp = _request_with_rate_limit("GET", "https://example.invalid", {}, max_retries=2)

    assert resp.status_code == 599
    assert "simulated timeout" in resp.text


def test_timeout_then_success_recovers_without_raising(monkeypatch):
    calls = {"n": 0}

    class _Ok:
        status_code = 200
        text = "ok"

    def _fails_once_then_succeeds(method, url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.exceptions.ConnectionError("simulated reset")
        return _Ok()

    monkeypatch.setattr(requests, "request", _fails_once_then_succeeds)
    monkeypatch.setattr("store_pricing.apple._rate_limiter", _RateLimiter(min_interval=0.0))

    resp = _request_with_rate_limit("GET", "https://example.invalid", {}, max_retries=3)

    assert resp.status_code == 200
    assert calls["n"] == 2
