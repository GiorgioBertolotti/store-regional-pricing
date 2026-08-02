"""Google Play price updates via the Android Publisher API.

Prices are written by patching the full subscription object in-place (there is no
per-region endpoint), which means every patch resubmits *all* base plans, not just the one
being targeted - a pre-existing data problem on a base plan you have no intention of
touching (e.g. a stale price left over from a currency changeover) can still block the
whole patch. The 400-error recovery ladder below (_handle_400_error and friends) exists
because of that: it auto-fixes stale currency codes, drops non-billable regions, and
clamps out-of-range prices to Google's minimum, retrying until the patch validates or a
genuine data lock is hit.

Ported from subscription_price_applier.py with one structural change: every function here
takes GoogleCreds explicitly instead of reading module-level globals frozen at import time,
so credentials can be constructed by a wizard and used in the same process.
"""

from __future__ import annotations

import copy
import re

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from store_pricing.config import GoogleCreds, PricingConfig
from store_pricing.inputs import InputData, get_alpha_2_country_code
from store_pricing.report import make_failure
from store_pricing.scaler import convert_to_currency, fetch_all_usd_rates

# These parse Google's human-readable 400 error strings to identify recoverable errors.
# If Google changes the wording the regex won't match, and the HttpError re-raises unchanged - safe fallback.
_CURRENCY_MISMATCH_RE = re.compile(r"region code (\w+).*?Expected (\w+) but got (\w+)")
_NOT_BILLABLE_RE = re.compile(r"Region code (\w+) is not billable")
_PRICE_OUT_OF_RANGE_RE = re.compile(r"([\w-]+): Price for (\w+) must be between [^\d]*([\d.]+)")
_REGION_REMOVAL_REJECTED_RE = re.compile(r"([\w-]+): Regional configs were removed from the base plan: (\w+)")


def _price_to_units_nanos(price: float) -> tuple:
    # Round to whole cents first via divmod so a carry (e.g. 99.5 cents rounding
    # up to 100) rolls into the unit instead of producing an out-of-range nanos
    # value - Google's Money type requires 0 <= nanos <= 999_999_999.
    total_cents = round(price * 100)
    units, cents = divmod(total_cents, 100)
    nanos = cents * int(1e7)
    return str(units), nanos


def _make_failure(country, country_code, price, currency, reason) -> dict:
    return make_failure("Google Play", country, country_code, price, currency, reason)


def build_service(creds: GoogleCreds):
    credentials = service_account.Credentials.from_service_account_file(
        creds.service_account_file,
        scopes=["https://www.googleapis.com/auth/androidpublisher"],
    )
    return build("androidpublisher", "v3", credentials=credentials)


def fetch_subscription(service, creds: GoogleCreds) -> "tuple | None":
    """Fetch subscription from Play Console. Returns (subscription_response, regions_version) or None."""
    try:
        response = (
            service.monetization()
            .subscriptions()
            .get(packageName=creds.package_name, productId=creds.subscription_id)
            .execute()
        )
        regions_version = response.get("regionsVersion", "2025/01")
        if regions_version == "1":
            regions_version = "2025/01"
        return response, regions_version
    except Exception as e:
        print(f"Error getting subscription details: {e}")
        return None


def fetch_live_prices(creds: GoogleCreds, data: InputData, base_plan_id: "str | None" = None) -> dict[str, dict]:
    """Return {country_name: {"price": float, "currency": str}} for the current live
    regional configs of the given base plan (defaults to creds.baseplan_id) - used for the
    apply-time diff against what's about to be pushed.

    Google's regional configs are keyed by ISO alpha-2 region code, but everything else in
    this pipeline (InputData.country_prices, the diff table) is keyed by country name - so
    this reverses country_code_mapping to translate as it reads, rather than pushing that
    mismatch onto every caller.

    The reverse map is built only from `data.country_prices` (the countries we're actually
    pricing), not from every entry in country_code_mapping. country_codes.json accumulates
    naming variants over time since its refresh is merge-only and never deletes an old
    entry - e.g. both "United States" and "United States of America" map to alpha2 "US",
    "Korea, Rep." and "South Korea" both map to "KR". Reversing the *whole* file would pick
    whichever variant happens to sort last, which often isn't the exact name
    price_scaled.xlsx uses - silently losing that country's live price (it would show as
    "new" in the diff even though it's actually live).
    """
    service = build_service(creds)
    result = fetch_subscription(service, creds)
    if result is None:
        return {}
    subscription, _ = result
    base_plan_id = base_plan_id or creds.baseplan_id

    alpha2_to_name = {}
    for country in data.country_prices:
        code = get_alpha_2_country_code(country, data.country_code_mapping)
        if code:
            alpha2_to_name[code] = country

    live = {}
    for bp in subscription.get("basePlans", []):
        if bp.get("basePlanId") != base_plan_id:
            continue
        for config in bp.get("regionalConfigs", []):
            country_name = alpha2_to_name.get(config.get("regionCode"))
            if not country_name:
                continue
            price = config.get("price", {})
            units = float(price.get("units", 0))
            nanos = price.get("nanos", 0)
            live[country_name] = {
                "price": units + nanos / 1e9,
                "currency": price.get("currencyCode"),
            }
    return live


def _update_region(
    subscription_update, creds: GoogleCreds, country, country_code, currency_code, price,
    usd_price: "float | None" = None, usd_rates: "dict | None" = None, rounding: str = "psychological",
) -> "dict | None":
    price_units, price_nanos = _price_to_units_nanos(float(price))

    for base_plan in subscription_update.get("basePlans", []):
        if base_plan.get("basePlanId") != creds.baseplan_id:
            continue

        for config in base_plan.get("regionalConfigs", []):
            if config.get("regionCode") != country_code:
                continue

            existing_currency = config.get("price", {}).get("currencyCode")
            if existing_currency != currency_code:
                # Google rejects any price update whose currency doesn't match what's
                # already configured for that region (no in-place currency change via this
                # endpoint) - usually a leftover from an earlier flat-price rollout that
                # never localized this region. Rather than fail outright, convert our
                # computed price into whatever currency Google already has on file, the
                # same way Apple's resolver retries in USD for territories it prices
                # differently (see apple.resolve_price_point()).
                converted = convert_to_currency(usd_price, existing_currency, usd_rates or {}, rounding)
                if converted is None:
                    reason = (
                        f"Currency mismatch: Google expects {existing_currency}, we have {currency_code} "
                        f"(couldn't auto-convert - no USD price or exchange rate available)"
                    )
                    print(f"   Skipping {country_code} - {reason}")
                    return _make_failure(country, country_code, price, currency_code, reason)

                print(f"   Currency mismatch for {country_code}: Google expects {existing_currency} - "
                      f"converting {price} {currency_code} -> {converted} {existing_currency}")
                conv_units, conv_nanos = _price_to_units_nanos(converted)
                config["price"] = {"currencyCode": existing_currency, "units": conv_units, "nanos": conv_nanos}
                return None

            config["price"] = {"currencyCode": currency_code, "units": price_units, "nanos": price_nanos}
            return None

        reason = "Region not found in Google Play configuration"
        print(f"   Skipping {country_code} - {reason}")
        return _make_failure(country, country_code, price, currency_code, reason)

    return _make_failure(
        country, country_code, price, currency_code,
        f"Base plan {creds.baseplan_id} not found in subscription",
    )


def _fix_currency_mismatch(subscription_update, region_code, expected_currency, got_currency) -> None:
    for bp in subscription_update.get("basePlans", []):
        for config in bp.get("regionalConfigs", []):
            if config.get("regionCode") == region_code:
                if config.get("price", {}).get("currencyCode") == got_currency:
                    config["price"]["currencyCode"] = expected_currency


def _fix_not_billable(subscription_update, region_code) -> None:
    for bp in subscription_update.get("basePlans", []):
        bp["regionalConfigs"] = [c for c in bp.get("regionalConfigs", []) if c.get("regionCode") != region_code]


def _remove_region_from_base_plan(subscription_update, base_plan_id, region_code) -> None:
    for bp in subscription_update.get("basePlans", []):
        if bp.get("basePlanId") == base_plan_id:
            bp["regionalConfigs"] = [c for c in bp.get("regionalConfigs", []) if c.get("regionCode") != region_code]


def _fix_price_out_of_range(subscription_update, base_plan_id, region_code, min_price_str) -> tuple:
    """Clamp region price to the API's minimum. Returns (min_price, failure_dict)."""
    try:
        min_price = float(min_price_str)
    except ValueError as e:
        raise RuntimeError(f"Could not parse minimum price '{min_price_str}' from API error") from e

    price_units, price_nanos = _price_to_units_nanos(min_price)
    for bp in subscription_update.get("basePlans", []):
        if bp.get("basePlanId") == base_plan_id:
            for config in bp.get("regionalConfigs", []):
                if config.get("regionCode") == region_code:
                    config["price"]["units"] = price_units
                    config["price"]["nanos"] = price_nanos

    failure = _make_failure(region_code, region_code, min_price, None, f"Price clamped to Google Play minimum ({min_price})")
    return min_price, failure


def _handle_400_error(subscription_update, error_str, attempt, extra_failures, auto_fixed_regions, clamped_pairs) -> bool:
    """Handle a recoverable Play Console 400 error. Returns True if handled, False to re-raise."""
    currency_match = _CURRENCY_MISMATCH_RE.search(error_str)
    if currency_match:
        region_code, expected_currency, got_currency = currency_match.groups()
        print(f"   Auto-fixing stale currency for {region_code}: {got_currency} → {expected_currency} (attempt {attempt + 1})")
        _fix_currency_mismatch(subscription_update, region_code, expected_currency, got_currency)
        auto_fixed_regions.add(region_code)
        return True

    billable_match = _NOT_BILLABLE_RE.search(error_str)
    if billable_match:
        region_code = billable_match.group(1)
        print(f"   Removing non-billable region {region_code} (attempt {attempt + 1})")
        _fix_not_billable(subscription_update, region_code)
        return True

    removal_rejected_match = _REGION_REMOVAL_REJECTED_RE.search(error_str)
    if removal_rejected_match:
        base_plan_id, region_code = removal_rejected_match.groups()
        raise RuntimeError(
            f"Google Play won't accept a fix for {region_code} on base plan '{base_plan_id}' via this API: "
            f"its price fails Google's own validation, but the API also refuses to let us remove that "
            f"region's config to work around it. This needs a manual fix in Play Console "
            f"(Monetize > Products > your subscription > {base_plan_id} > {region_code} pricing) before "
            f"any price updates can go through - Google requires the whole basePlans object to validate "
            f"together, so this one broken entry blocks every base plan's price update, not just its own."
        )

    price_range_match = _PRICE_OUT_OF_RANGE_RE.search(error_str)
    if price_range_match:
        base_plan_id, region_code, min_price_str = price_range_match.groups()
        detail = error_str.split("Details:")[0].strip()
        pair = (base_plan_id, region_code)

        if pair in clamped_pairs:
            # We already clamped this exact (base plan, region) once and Google is reporting
            # the identical violation again - the clamp isn't converging (likely a pre-existing
            # data inconsistency on that base plan, unrelated to what this run is updating).
            # Drop the region from just that base plan so the rest of the patch can go through.
            print(f"   Clamp for {region_code} in {base_plan_id} didn't stick - removing that region from {base_plan_id} instead (attempt {attempt + 1}): {detail}")
            _remove_region_from_base_plan(subscription_update, base_plan_id, region_code)
            extra_failures.append(_make_failure(
                region_code, region_code, None, None,
                f"Removed from {base_plan_id}: price kept failing validation after clamping ({detail})",
            ))
            return True

        print(f"   Clamping {region_code} in {base_plan_id} to minimum price (attempt {attempt + 1}): {detail}")
        min_price, failure = _fix_price_out_of_range(subscription_update, base_plan_id, region_code, min_price_str)
        clamped_pairs.add(pair)
        failure["reason"] = f"Price clamped to Google Play minimum ({min_price}): {detail}"
        extra_failures.append(failure)
        return True

    return False


def patch_with_currency_fixes(service, creds: GoogleCreds, subscription_update, regions_version, max_retries=15) -> tuple:
    """Submit the Play Console subscription patch, retrying on recoverable 400 errors.

    Returns (auto_fixed_regions, extra_failures).
    """
    auto_fixed_regions = set()
    extra_failures = []
    clamped_pairs = set()

    for attempt in range(max_retries):
        request = (
            service.monetization()
            .subscriptions()
            .patch(
                packageName=creds.package_name,
                productId=creds.subscription_id,
                allowMissing=False,
                updateMask="basePlans",
                body=subscription_update,
            )
        )
        sep = "&" if "?" in request.uri else "?"
        request.uri += f"{sep}regionsVersion.version={regions_version}"

        try:
            request.execute()
            return auto_fixed_regions, extra_failures
        except HttpError as e:
            if e.resp.status != 400:
                raise
            if not _handle_400_error(subscription_update, str(e), attempt, extra_failures, auto_fixed_regions, clamped_pairs):
                raise

    raise RuntimeError(f"Could not resolve all API errors after {max_retries} retries")


def update_prices(creds: GoogleCreds, config: PricingConfig, data: InputData, dry_run: bool = False) -> list[dict]:
    """Push data.country_prices to Google Play. In dry-run mode, builds the same patch
    body and reports what would change but never calls .patch()."""
    service = build_service(creds)

    result = fetch_subscription(service, creds)
    if result is None:
        return [_make_failure(
            "N/A", "N/A", None, None,
            "Could not fetch subscription from Google Play - no prices were updated (see console output above)",
        )]
    subscription_response, regions_version = result
    subscription_update = copy.deepcopy(subscription_response)

    try:
        usd_rates = fetch_all_usd_rates()
    except Exception as e:
        print(f"Warning: could not fetch exchange rates ({e}) - currency-mismatched regions will fail instead of auto-converting")
        usd_rates = {}

    failures = []
    for country, price in data.country_prices.items():
        country_code = None
        try:
            country_code = get_alpha_2_country_code(country, data.country_code_mapping)
            currency_code = data.country_currencies.get(country, "USD")
            print(f"{'[dry-run] Would update' if dry_run else 'Updating'} Google Play price for {country} ({country_code}): {price} {currency_code}")
            failure = _update_region(
                subscription_update, creds, country, country_code, currency_code, price,
                usd_price=data.country_prices_usd.get(country), usd_rates=usd_rates, rounding=config.rounding,
            )
            if failure:
                failures.append(failure)
        except Exception as e:
            print(f"Error updating {country}: {e}")
            failures.append(_make_failure(
                country, country_code or "Unknown", price,
                data.country_currencies.get(country, "Unknown"), f"Exception: {e}",
            ))

    if dry_run:
        print(f"[dry-run] Would patch base plan '{creds.baseplan_id}' - no request sent")
        return failures

    auto_fixed, patch_failures = patch_with_currency_fixes(service, creds, subscription_update, regions_version)
    if auto_fixed:
        failures = [f for f in failures if f.get("country_code") not in auto_fixed]
    failures.extend(patch_failures)
    print("Successfully updated Google Play prices")
    return failures
