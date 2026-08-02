"""diff_table() classifies each row as unchanged / applyable / currency-locked. Google
rejects any price update whose currency doesn't match what's already live on that region
(no retry - see google._update_region()), so a currency difference there is a predictable
failure, not just informational noise; Apple resolves per-territory regardless of what was
previously live, so the same difference there never blocks anything. These tests pin that
distinction down since it's easy to regress back into treating every currency difference
the same way (which is what originally buried real price changes under ~130 identical
"currency change" rows for a subscription with legacy flat-USD-per-region pricing)."""

from store_pricing.ui import diff_table


def test_same_currency_price_change_is_applyable_with_percent_delta():
    live = {"Germany": {"price": 9.99, "currency": "EUR"}}
    table, applyable, locked = diff_table("Google Play", live, {"Germany": 12.99}, {"Germany": "EUR"}, currency_mismatch_blocks=True)

    assert applyable == 1
    assert locked == 0
    assert "Germany" in _rendered(table)


def test_identical_price_and_currency_is_unchanged_and_not_counted():
    live = {"Germany": {"price": 9.99, "currency": "EUR"}}
    table, applyable, locked = diff_table("Google Play", live, {"Germany": 9.99}, {"Germany": "EUR"}, currency_mismatch_blocks=True)

    assert applyable == 0
    assert locked == 0
    assert table.caption and "1 unchanged" in table.caption


def test_no_live_entry_is_new_and_applyable():
    table, applyable, locked = diff_table("Google Play", {}, {"Norway": 199.0}, {"Norway": "NOK"}, currency_mismatch_blocks=True)

    assert applyable == 1
    assert locked == 0


def test_google_currency_mismatch_is_locked_not_applyable():
    # Google's regional config is stuck on USD from a legacy flat rollout; our computed
    # price is properly localized to NAD. Google will reject this outright (no retry path
    # exists in google._update_region for a currency mismatch) - it must NOT be counted as
    # an ordinary applyable change.
    live = {"Namibia": {"price": 19.99, "currency": "USD"}}
    table, applyable, locked = diff_table("Google Play", live, {"Namibia": 170.0}, {"Namibia": "NAD"}, currency_mismatch_blocks=True)

    assert applyable == 0
    assert locked == 1
    assert "locked" in _rendered(table)


def test_google_currency_mismatch_with_a_usable_rate_is_fx_convert_not_locked():
    # Same shape as test_google_currency_mismatch_is_locked_not_applyable, but this time a
    # USD rate is available - google._update_region() would successfully convert this, so
    # the diff must predict "fx convert" (applyable), not "locked".
    live = {"Namibia": {"price": 19.99, "currency": "USD"}}
    table, applyable, locked = diff_table(
        "Google Play", live, {"Namibia": 170.0}, {"Namibia": "NAD"}, currency_mismatch_blocks=True,
        usd_rates={"USD": 1.0}, country_prices_usd={"Namibia": 10.49},
    )

    assert applyable == 1
    assert locked == 0
    assert "fx convert" in _rendered(table)


def test_apple_currency_difference_is_applyable_not_locked():
    # Same shape of divergence as the Google case, but Apple resolves per-territory
    # regardless of the previously-live currency (falling back to USD when needed) - a
    # difference here never blocks the update, so it must NOT be counted as locked.
    live = {"Namibia": {"price": 10.49, "currency": "USD"}}
    table, applyable, locked = diff_table("Apple App Store", live, {"Namibia": 170.0}, {"Namibia": "NAD"}, currency_mismatch_blocks=False)

    assert applyable == 1
    assert locked == 0
    assert "fx change" in _rendered(table)


def test_locked_rows_are_excluded_from_unchanged_and_from_the_table_rows_with_other_status():
    live = {
        "Germany": {"price": 12.99, "currency": "EUR"},   # unchanged
        "Namibia": {"price": 19.99, "currency": "USD"},   # locked
        "Norway": {"price": 199.0, "currency": "NOK"},    # applyable, % change
    }
    new_prices = {"Germany": 12.99, "Namibia": 170.0, "Norway": 235.0}
    currencies = {"Germany": "EUR", "Namibia": "NAD", "Norway": "NOK"}

    table, applyable, locked = diff_table("Google Play", live, new_prices, currencies, currency_mismatch_blocks=True)

    assert applyable == 1
    assert locked == 1
    rendered = _rendered(table)
    assert "Norway" in rendered
    assert "Namibia" in rendered
    assert "1 unchanged" in table.caption
    assert "1 currency-locked" in table.caption


def _rendered(table) -> str:
    from io import StringIO
    from rich.console import Console

    buf = StringIO()
    Console(file=buf, width=120).print(table)
    return buf.getvalue()
