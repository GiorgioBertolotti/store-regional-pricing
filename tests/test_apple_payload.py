"""Asserts the JSON:API compound-document shape Apple's runtime validation demands for
subscriptionPromotionalOffers, which isn't reflected in Apple's published OpenAPI schema
and was reverse-engineered from live 409 error responses (see offers.py's module
docstring). If this shape regresses, offer creation fails opaquely against the real API -
this test catches it locally instead."""

from store_pricing import offers as offers_mod
from store_pricing.config import AppleCreds, PricingConfig
from store_pricing.inputs import InputData


class _FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def _apple_creds() -> AppleCreds:
    return AppleCreds(
        issuer_id="11111111-1111-1111-1111-111111111111",
        key_id="ABC123",
        private_key="-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----",
        app_id="123456789",
        subscription_product_id="premium_monthly",
    )


def _input_data() -> InputData:
    return InputData(
        country_prices={"Norway": 99.0},
        country_prices_usd={"Norway": 9.99},
        country_currencies={"Norway": "NOK"},
        country_code_mapping={"Norway": {"alpha2": "NO", "alpha3": "NOR"}},
    )


def test_apple_promo_offer_payload_shape(monkeypatch):
    captured = {}

    monkeypatch.setattr(offers_mod, "get_jwt", lambda creds: "token")
    monkeypatch.setattr(offers_mod, "get_subscription_id", lambda token, creds: "sub_123")
    monkeypatch.setattr(offers_mod, "_find_apple_offer_id", lambda base_url, headers, subscription_id, offer_code: None)
    monkeypatch.setattr(offers_mod, "fetch_all_usd_rates", lambda: {"NOK": 10.5})

    def fake_resolve(token, subscription_id, country, country_code, price, currency_code, usd_prices, usd_rates=None, rounding="psychological"):
        return {"id": "pp_1", "attributes": {"customerPrice": "69.99"}}, price, currency_code, currency_code, None

    monkeypatch.setattr(offers_mod, "resolve_price_point", fake_resolve)

    def fake_post(url, headers, body):
        captured["url"] = url
        captured["body"] = body
        return _FakeResponse(201)

    # Patched at the offers-module wrapper rather than requests.post: every Apple call in
    # offers.py goes through apple.py's shared rate limiter now, so requests.post is no
    # longer what gets invoked.
    monkeypatch.setattr(offers_mod, "_post_apple", fake_post)

    config = offers_mod.OfferConfig(
        platforms={"apple"}, discount_percent=30, num_periods=1,
        apple_duration="ONE_MONTH", google_duration="P1M",
        offer_name="Summer sale", offer_code="SUMMER30",
    )

    failures = offers_mod.create_apple_offer(_apple_creds(), PricingConfig(), _input_data(), config)

    assert failures == []
    assert captured["url"].endswith("/subscriptionPromotionalOffers")

    body = captured["body"]
    data = body["data"]
    assert data["type"] == "subscriptionPromotionalOffers"
    assert data["attributes"]["offerCode"] == "SUMMER30"
    assert data["attributes"]["offerMode"] == "PAY_AS_YOU_GO"
    assert data["relationships"]["subscription"]["data"]["id"] == "sub_123"

    # Apple requires the "prices" relationship to reference client-scoped ids wrapped as
    # "${local-id}" - a plain territory code like "NOR" is rejected with
    # ENTITY_ERROR.INCLUDED.INVALID_ID.
    price_refs = data["relationships"]["prices"]["data"]
    assert price_refs == [{"type": "subscriptionPromotionalOfferPrices", "id": "${NOR}"}]

    # ...and a top-level "included" array must supply, for that same id, a
    # subscriptionPricePoint + territory relationship pair.
    included = body["included"]
    assert len(included) == 1
    assert included[0]["id"] == "${NOR}"
    assert included[0]["relationships"]["subscriptionPricePoint"]["data"]["id"] == "pp_1"
    assert included[0]["relationships"]["territory"]["data"]["id"] == "NOR"


def test_apple_promo_offer_skips_countries_without_price_point(monkeypatch):
    monkeypatch.setattr(offers_mod, "get_jwt", lambda creds: "token")
    monkeypatch.setattr(offers_mod, "get_subscription_id", lambda token, creds: "sub_123")
    monkeypatch.setattr(offers_mod, "_find_apple_offer_id", lambda base_url, headers, subscription_id, offer_code: None)
    monkeypatch.setattr(offers_mod, "fetch_all_usd_rates", lambda: {})
    monkeypatch.setattr(offers_mod, "resolve_price_point", lambda *a, **k: (None, None, None, "EUR", None))

    config = offers_mod.OfferConfig(
        platforms={"apple"}, discount_percent=30, num_periods=1,
        apple_duration="ONE_MONTH", google_duration="P1M",
        offer_name="Summer sale", offer_code="SUMMER30",
    )

    failures = offers_mod.create_apple_offer(_apple_creds(), PricingConfig(), _input_data(), config)

    assert len(failures) == 1
    assert failures[0]["platform"] == "Apple App Store"
    assert "No price points" in failures[0]["reason"]
