"""apple.resolve_price_point() used to only ever retry in USD when Apple prices a
territory differently than the local currency we computed - anything else (observed live:
Apple prices Serbia, Bosnia, and Bulgaria in EUR, not USD) was skipped outright, even
though a matching EUR price point likely existed. It now converts into whatever currency
Apple actually reports via convert_to_currency() (the same conversion google.py uses for
currency-locked Google regions), so any currency Apple uses gets a real retry, not just
USD."""

from store_pricing.apple import resolve_price_point


def test_retries_in_whatever_currency_apple_reports_not_only_usd(monkeypatch):
    calls = []

    def fake_get_closest_price_point(token, subscription_id, country_code, target_price, expected_currency=None):
        calls.append((target_price, expected_currency))
        if expected_currency == "RSD":
            # First call: our currency (RSD) doesn't match what Apple has for this territory.
            return None, "EUR", None
        if expected_currency == "EUR":
            # Retry call: Apple's actual currency (EUR) finds a match.
            return {"id": "pp_eur", "attributes": {"customerPrice": "9.99"}}, "EUR", None
        raise AssertionError(f"unexpected currency {expected_currency}")

    monkeypatch.setattr("store_pricing.apple.get_closest_price_point", fake_get_closest_price_point)

    closest_point, final_price, final_currency, apple_currency, api_error = resolve_price_point(
        token="token", subscription_id="sub", country="Serbia", country_code="SRB",
        price=1220.0, currency_code="RSD", country_prices_usd={"Serbia": 9.99},
        usd_rates={"EUR": 0.92}, rounding="none",
    )

    assert closest_point is not None
    assert closest_point["id"] == "pp_eur"
    assert final_currency == "EUR"
    assert apple_currency == "EUR"
    # First call used our RSD price/currency, retry used the converted EUR amount.
    assert calls[0] == (1220.0, "RSD")
    assert calls[1][1] == "EUR"
    assert calls[1][0] > 0


def test_no_retry_without_a_usable_rate_for_apples_currency(monkeypatch):
    def fake_get_closest_price_point(token, subscription_id, country_code, target_price, expected_currency=None):
        return None, "EUR", None

    monkeypatch.setattr("store_pricing.apple.get_closest_price_point", fake_get_closest_price_point)

    closest_point, final_price, final_currency, apple_currency, api_error = resolve_price_point(
        token="token", subscription_id="sub", country="Serbia", country_code="SRB",
        price=1220.0, currency_code="RSD", country_prices_usd={"Serbia": 9.99},
        usd_rates={}, rounding="none",  # no EUR rate available
    )

    assert closest_point is None
    assert apple_currency == "EUR"


def test_still_works_when_apple_uses_usd_specifically(monkeypatch):
    calls = []

    def fake_get_closest_price_point(token, subscription_id, country_code, target_price, expected_currency=None):
        calls.append(expected_currency)
        if expected_currency == "NAD":
            return None, "USD", None
        return {"id": "pp_usd", "attributes": {"customerPrice": "10.49"}}, "USD", None

    monkeypatch.setattr("store_pricing.apple.get_closest_price_point", fake_get_closest_price_point)

    closest_point, final_price, final_currency, apple_currency, api_error = resolve_price_point(
        token="token", subscription_id="sub", country="Namibia", country_code="NAM",
        price=170.0, currency_code="NAD", country_prices_usd={"Namibia": 10.49},
        usd_rates={"USD": 1.0}, rounding="none",
    )

    assert closest_point["id"] == "pp_usd"
    assert apple_currency == "USD"
    assert calls == ["NAD", "USD"]
