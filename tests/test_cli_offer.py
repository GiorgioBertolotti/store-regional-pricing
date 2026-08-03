"""`offer --code` doubles as the Google Play offer id, but the two stores have
incompatible id rules: Apple offer codes are conventionally uppercase (SUMMER30) while
Google Play rejects anything outside [a-z0-9-]. Passing one `--code` for both - which is
what CLAUDE.md's own example does - reached Google as an invalid id and came back as a
400 buried in the failure report, after the Apple half had already been created.

It's now caught up front, before any store is contacted, with --google-offer-id as the
way to set them independently.
"""

import pytest

from store_pricing import cli
from store_pricing.config import AppleCreds, GoogleCreds, PricingConfig, Settings, StripeCreds
from store_pricing.inputs import InputData


def _settings(google=True, apple=True, stripe=False):
    return Settings(
        google=GoogleCreds("sa.json", "com.acme.app", "premium", "monthly") if google else None,
        apple=AppleCreds("issuer", "key", "-----BEGIN PRIVATE KEY-----", "123", "premium") if apple else None,
        stripe=StripeCreds("sk_test_x", "price_123") if stripe else None,
        pricing=PricingConfig(),
    )


def _input_data():
    return InputData(
        country_prices={"Norway": 99.0},
        country_prices_usd={"Norway": 9.99},
        country_currencies={"Norway": "NOK"},
        country_code_mapping={"Norway": {"alpha2": "NO", "alpha3": "NOR"}},
    )


@pytest.fixture
def offer_env(monkeypatch):
    """Stub out everything that would touch the network or the filesystem, and make any
    interactive prompt a hard failure - these paths must be fully non-interactive."""
    monkeypatch.setattr(cli, "load_input_data", _input_data)
    monkeypatch.setattr(cli.offers_mod, "find_existing_apple_offer", lambda creds, code: None)

    created = {}

    def _record_google(creds, data, config, dry_run=False):
        created["google"] = config
        return []

    def _record_apple(creds, pricing, data, config, dry_run=False):
        created["apple"] = config
        return []

    def _record_stripe(creds, config, dry_run=False):
        created["stripe"] = config
        return []

    monkeypatch.setattr(cli.offers_mod, "create_google_offer", _record_google)
    monkeypatch.setattr(cli.offers_mod, "create_apple_offer", _record_apple)
    monkeypatch.setattr(cli.offers_mod, "create_stripe_offer", _record_stripe)

    def _no_prompts(*args, **kwargs):
        raise AssertionError("should not prompt when every value is supplied via flags")

    monkeypatch.setattr(cli, "text", _no_prompts)
    monkeypatch.setattr(cli, "select", _no_prompts)
    monkeypatch.setattr(cli, "confirm", _no_prompts)
    monkeypatch.setattr(cli, "checkbox", _no_prompts)
    return created


def _run(monkeypatch, argv, settings):
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    parser = cli.build_parser()
    return cli.cmd_offer(parser.parse_args(argv))


def test_uppercase_code_is_rejected_before_contacting_google(offer_env, monkeypatch):
    exit_code = _run(
        monkeypatch,
        ["offer", "--google", "--discount", "30", "--cycles", "3", "--period", "P1M", "--code", "SUMMER30"],
        _settings(),
    )

    assert exit_code == 1
    assert "google" not in offer_env  # nothing was created


def test_google_offer_id_can_be_set_independently_of_the_apple_code(offer_env, monkeypatch):
    exit_code = _run(
        monkeypatch,
        ["offer", "--apple", "--google", "--discount", "30", "--cycles", "3", "--period", "P1M",
         "--code", "SUMMER30", "--google-offer-id", "summer-30-off", "--name", "Summer sale"],
        _settings(),
    )

    assert exit_code == 0
    assert offer_env["apple"].offer_code == "SUMMER30"
    assert offer_env["google"].google_offer_id == "summer-30-off"


def test_a_lowercase_code_still_serves_both_stores(offer_env, monkeypatch):
    exit_code = _run(
        monkeypatch,
        ["offer", "--apple", "--google", "--discount", "30", "--cycles", "1", "--period", "P1M",
         "--code", "summer-30", "--name", "Summer sale"],
        _settings(),
    )

    assert exit_code == 0
    assert offer_env["google"].google_offer_id == "summer-30"
    assert offer_env["apple"].offer_code == "summer-30"


def test_an_apple_only_offer_ignores_google_id_rules(offer_env, monkeypatch):
    exit_code = _run(
        monkeypatch,
        ["offer", "--apple", "--discount", "30", "--cycles", "1", "--period", "P1M",
         "--code", "SUMMER30", "--name", "Summer sale"],
        _settings(),
    )

    assert exit_code == 0
    assert offer_env["apple"].offer_code == "SUMMER30"
    assert "google" not in offer_env


def test_period_accepts_either_the_apple_enum_or_the_google_iso_period(offer_env, monkeypatch):
    for period in ("P1M", "ONE_MONTH"):
        offer_env.clear()
        exit_code = _run(
            monkeypatch,
            ["offer", "--apple", "--discount", "30", "--cycles", "1", "--period", period,
             "--code", "summer", "--name", "Summer sale"],
            _settings(),
        )
        assert exit_code == 0
        assert offer_env["apple"].apple_duration == "ONE_MONTH"
        assert offer_env["apple"].google_duration == "P1M"


def test_unknown_period_is_rejected(offer_env, monkeypatch):
    exit_code = _run(
        monkeypatch,
        ["offer", "--apple", "--discount", "30", "--cycles", "1", "--period", "P4M",
         "--code", "summer", "--name", "Summer sale"],
        _settings(),
    )
    assert exit_code == 1


def test_stripe_reuses_the_apple_style_code_as_coupon_and_promo_code(offer_env, monkeypatch):
    exit_code = _run(
        monkeypatch,
        ["offer", "--stripe", "--discount", "30", "--cycles", "3", "--period", "P1M",
         "--code", "SUMMER30", "--name", "Summer sale"],
        _settings(google=False, apple=False, stripe=True),
    )

    assert exit_code == 0
    assert offer_env["stripe"].offer_code == "SUMMER30"
    assert offer_env["stripe"].num_periods == 3
    assert "google" not in offer_env
    assert "apple" not in offer_env


def test_weekly_period_is_rejected_for_stripe_before_contacting_it(offer_env, monkeypatch):
    exit_code = _run(
        monkeypatch,
        ["offer", "--stripe", "--discount", "30", "--cycles", "1", "--period", "P1W",
         "--code", "summer", "--name", "Summer sale"],
        _settings(google=False, apple=False, stripe=True),
    )

    assert exit_code == 1
    assert "stripe" not in offer_env


def test_stripe_and_apple_together_share_one_offer_code_prompt_pass(offer_env, monkeypatch):
    exit_code = _run(
        monkeypatch,
        ["offer", "--apple", "--stripe", "--discount", "30", "--cycles", "1", "--period", "P1M",
         "--code", "SUMMER30", "--name", "Summer sale"],
        _settings(google=False, apple=True, stripe=True),
    )

    assert exit_code == 0
    assert offer_env["apple"].offer_code == "SUMMER30"
    assert offer_env["stripe"].offer_code == "SUMMER30"
