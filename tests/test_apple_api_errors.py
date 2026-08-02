"""A throttled or timed-out Apple request must not be reported as "this territory has no
matching price point".

The shared rate limiter narrows the window but can't close it: after `max_retries`
exhausted 429s - or a persistent ReadTimeout, which comes back as _FailedRequest with
status 599 - get_closest_price_point() still ends up empty-handed. Reporting that as "no
price points available" tells the operator the territory is unpriceable when the real
answer is "re-run it", and those countries then get silently written off.
"""

from store_pricing import apple as apple_api
from store_pricing.apple import get_closest_price_point, resolve_price_point
from store_pricing.config import PricingConfig
from store_pricing.inputs import InputData
from store_pricing.ui import _categorize_failure


class _Resp:
    def __init__(self, status_code, text="", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload or {}

    def json(self):
        return self._payload


def _input_data():
    return InputData(
        country_prices={"Norway": 99.0},
        country_prices_usd={"Norway": 9.99},
        country_currencies={"Norway": "NOK"},
        country_code_mapping={"Norway": {"alpha2": "NO", "alpha3": "NOR"}},
    )


# --- get_closest_price_point(): transport failure vs genuinely empty ---

def test_rate_limit_surfaces_as_an_api_error_not_an_empty_result(monkeypatch):
    monkeypatch.setattr(apple_api, "_get_with_rate_limit",
                        lambda url, headers: _Resp(429, "RATE_LIMIT_EXCEEDED"))

    point, currency, api_error = get_closest_price_point("token", "sub", "NOR", 99.0, "NOK")

    assert point is None
    assert currency is None
    assert api_error is not None
    assert "429" in api_error


def test_persistent_timeout_surfaces_as_an_api_error(monkeypatch):
    # _FailedRequest is what _request_with_rate_limit returns when every attempt raised.
    monkeypatch.setattr(apple_api, "_get_with_rate_limit",
                        lambda url, headers: apple_api._FailedRequest("Request failed after 5 attempts: ReadTimeout"))

    point, currency, api_error = get_closest_price_point("token", "sub", "NOR", 99.0, "NOK")

    assert point is None
    assert "599" in api_error


def test_a_territory_apple_genuinely_cannot_price_reports_no_api_error(monkeypatch):
    monkeypatch.setattr(apple_api, "_get_with_rate_limit",
                        lambda url, headers: _Resp(200, payload={"data": [], "included": []}))

    point, currency, api_error = get_closest_price_point("token", "sub", "NOR", 99.0, "NOK")

    assert point is None
    assert api_error is None


# --- resolve_price_point(): the error must not be swallowed by the currency retry ---

def test_api_error_short_circuits_the_currency_retry(monkeypatch):
    calls = []

    def fake_get(token, subscription_id, country_code, target_price, expected_currency=None):
        calls.append(expected_currency)
        return None, None, "Apple API error 429: throttled"

    monkeypatch.setattr(apple_api, "get_closest_price_point", fake_get)

    point, price, currency, apple_currency, api_error = resolve_price_point(
        "token", "sub", "Norway", "NOR", 99.0, "NOK", {"Norway": 9.99},
        usd_rates={"USD": 1.0}, rounding="none",
    )

    assert point is None
    assert "429" in api_error
    # No point burning a second request on a retry when the first one never reached Apple.
    assert calls == ["NOK"]


def test_api_error_on_the_currency_retry_is_still_reported(monkeypatch):
    def fake_get(token, subscription_id, country_code, target_price, expected_currency=None):
        if expected_currency == "NOK":
            return None, "EUR", None  # currency mismatch, worth retrying
        return None, None, "Apple API error 429: throttled"

    monkeypatch.setattr(apple_api, "get_closest_price_point", fake_get)

    point, price, currency, apple_currency, api_error = resolve_price_point(
        "token", "sub", "Norway", "NOR", 99.0, "NOK", {"Norway": 9.99},
        usd_rates={"EUR": 0.92}, rounding="none",
    )

    assert point is None
    assert "429" in api_error


# --- the failure record and its report grouping ---

def test_update_prices_records_the_api_error_as_the_failure_reason(monkeypatch):
    monkeypatch.setattr(apple_api, "get_jwt", lambda creds: "token")
    monkeypatch.setattr(apple_api, "get_subscription_id", lambda token, creds: "sub_123")
    monkeypatch.setattr(apple_api, "fetch_all_usd_rates", lambda: {"USD": 1.0})
    monkeypatch.setattr(apple_api, "resolve_price_point",
                        lambda *a, **k: (None, 99.0, "NOK", None, "Apple API error 429: throttled"))

    failures = apple_api.update_prices(
        creds=None, config=PricingConfig(apple_max_workers=1), data=_input_data(),
    )

    assert len(failures) == 1
    assert "Apple API error" in failures[0]["reason"]
    assert "No price points" not in failures[0]["reason"]


def test_report_groups_transient_errors_apart_from_unpriceable_territories():
    transient = _categorize_failure("Apple API error 429: RATE_LIMIT_EXCEEDED")
    unpriceable = _categorize_failure("No price points available with currency NOK")

    assert transient != unpriceable
    assert "re-run" in transient
