"""Google rejects a price update whose currency doesn't match what's already configured
for that region (no in-place currency change via this endpoint) - usually a leftover from
an earlier flat-price rollout that never localized that region. Rather than fail outright,
_update_region() converts the computed price into whatever currency Google already has on
file, the same way apple.resolve_price_point() retries in USD for territories Apple prices
differently. These tests cover both the pure conversion helper and _update_region()'s use
of it, including the case where conversion genuinely isn't possible."""

import pytest

from store_pricing.config import GoogleCreds
from store_pricing.google import _update_region, convert_to_currency


# --- convert_to_currency() ---

def test_convert_to_currency_applies_rate_and_smart_rounding():
    # 20 USD * 0.85 EUR/USD = 17.0, smart-rounded to a .99 ending.
    converted = convert_to_currency(20.0, "EUR", {"EUR": 0.85}, rounding="psychological")
    assert converted == pytest.approx(16.99)


def test_convert_to_currency_none_rounding_is_exact():
    converted = convert_to_currency(20.0, "EUR", {"EUR": 0.85}, rounding="none")
    assert converted == pytest.approx(17.0)


def test_convert_to_currency_returns_none_without_usd_price():
    assert convert_to_currency(None, "EUR", {"EUR": 0.85}, rounding="psychological") is None


def test_convert_to_currency_returns_none_without_a_rate_for_the_target():
    assert convert_to_currency(20.0, "XYZ", {"EUR": 0.85}, rounding="psychological") is None


# --- _update_region() ---

def _creds() -> GoogleCreds:
    return GoogleCreds(service_account_file="unused.json", package_name="com.example.app",
                        subscription_id="sub", baseplan_id="annual")


def _subscription_update(existing_currency: str) -> dict:
    return {
        "basePlans": [{
            "basePlanId": "annual",
            "regionalConfigs": [
                {"regionCode": "NA", "price": {"currencyCode": existing_currency, "units": "19", "nanos": 990000000}},
            ],
        }],
    }


def test_currency_mismatch_is_converted_and_reported_as_success():
    # Google's Namibia region is still on the old flat USD price; our computed price is
    # properly localized to NAD. Converting into Google's expected currency (USD) should
    # succeed rather than failing outright - the rate table always carries "USD": 1.0
    # (rates are expressed relative to USD), same as the real exchangerate-api response.
    update = _subscription_update("USD")
    failure = _update_region(
        update, _creds(), "Namibia", "NA", "NAD", 170.0,
        usd_price=10.49, usd_rates={"USD": 1.0}, rounding="none",
    )

    assert failure is None
    config = update["basePlans"][0]["regionalConfigs"][0]
    assert config["price"]["currencyCode"] == "USD"  # Google's currency is unchanged, only the amount changes
    converted_price = int(config["price"]["units"]) + config["price"]["nanos"] / 1e9
    assert converted_price == pytest.approx(10.49, abs=0.01)


def test_currency_mismatch_without_a_rate_still_fails_with_an_explanatory_reason():
    update = _subscription_update("USD")
    failure = _update_region(
        update, _creds(), "Namibia", "NA", "NAD", 170.0,
        usd_price=10.49, usd_rates={}, rounding="none",
    )

    assert failure is not None
    assert failure["reason"].startswith("Currency mismatch")
    assert "couldn't auto-convert" in failure["reason"]
    # Untouched on failure.
    config = update["basePlans"][0]["regionalConfigs"][0]
    assert config["price"]["currencyCode"] == "USD"
    assert config["price"]["units"] == "19"


def test_currency_mismatch_without_a_usd_price_still_fails():
    update = _subscription_update("USD")
    failure = _update_region(
        update, _creds(), "Namibia", "NA", "NAD", 170.0,
        usd_price=None, usd_rates={"NAD": 18.0}, rounding="none",
    )

    assert failure is not None
    assert failure["reason"].startswith("Currency mismatch")


def test_matching_currency_updates_normally_without_conversion():
    update = _subscription_update("NAD")
    failure = _update_region(
        update, _creds(), "Namibia", "NA", "NAD", 170.0,
        usd_price=10.49, usd_rates={"NAD": 18.0}, rounding="none",
    )

    assert failure is None
    config = update["basePlans"][0]["regionalConfigs"][0]
    assert config["price"]["currencyCode"] == "NAD"
    price = int(config["price"]["units"]) + config["price"]["nanos"] / 1e9
    assert price == pytest.approx(170.0)
