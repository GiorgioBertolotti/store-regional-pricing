"""google.fetch_live_prices() has to reverse Google's region-code-keyed regionalConfigs
back into the country names price_scaled.xlsx uses. country_codes.json accumulates naming
variants over time (merge-only refresh, never deletes an old entry) - e.g. both "United
States" and "United States of America" map to alpha2 "US". Building the reverse map from
the *whole* mapping file picks whichever variant sorts last, which silently drops the live
price for whichever name price_scaled.xlsx actually uses. These tests pin down that the
reverse map must instead be scoped to exactly the countries being priced."""

from store_pricing.config import GoogleCreds
from store_pricing.google import fetch_live_prices
from store_pricing.inputs import InputData


def _creds() -> GoogleCreds:
    return GoogleCreds(service_account_file="unused.json", package_name="com.example.app",
                        subscription_id="sub", baseplan_id="annual")


def _subscription_response():
    return {
        "basePlans": [{
            "basePlanId": "annual",
            "regionalConfigs": [
                {"regionCode": "US", "price": {"currencyCode": "USD", "units": "19", "nanos": 990000000}},
                {"regionCode": "KR", "price": {"currencyCode": "KRW", "units": "20000", "nanos": 0}},
            ],
        }],
    }


def test_live_price_resolves_to_the_exact_name_price_scaled_uses_despite_alpha2_collision(monkeypatch):
    # country_codes.json has accumulated a naming-variant duplicate for both codes, same as
    # the real file's "United States" / "United States of America" and "Korea, Rep." /
    # "South Korea" collisions. price_scaled.xlsx (InputData.country_prices) uses the
    # World-Bank-style names ("United States", "Korea, Rep."), not the duplicates.
    country_code_mapping = {
        "United States": {"alpha2": "US", "alpha3": "USA"},
        "United States of America": {"alpha2": "US", "alpha3": "USA"},
        "Korea, Rep.": {"alpha2": "KR", "alpha3": "KOR"},
        "South Korea": {"alpha2": "KR", "alpha3": "KOR"},
    }
    data = InputData(
        country_prices={"United States": 19.99, "Korea, Rep.": 20230.0},
        country_prices_usd={"United States": 19.99, "Korea, Rep.": 20.23},
        country_currencies={"United States": "USD", "Korea, Rep.": "KRW"},
        country_code_mapping=country_code_mapping,
    )

    monkeypatch.setattr("store_pricing.google.build_service", lambda creds: object())
    monkeypatch.setattr("store_pricing.google.fetch_subscription", lambda service, creds: (_subscription_response(), "2025/01"))

    live = fetch_live_prices(_creds(), data)

    assert "United States" in live
    assert live["United States"]["price"] == 19.99
    assert "Korea, Rep." in live
    assert live["Korea, Rep."]["price"] == 20000.0

    # Must not have been misattributed to the duplicate names.
    assert "United States of America" not in live
    assert "South Korea" not in live
