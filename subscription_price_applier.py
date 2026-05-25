import copy
import os
import json
import re
from dataclasses import dataclass
import pandas as pd
import requests
import time
import jwt
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# --- CONFIG ---
EXCEL_FILE = "price_scaled.xlsx"

load_dotenv()

# Google Play
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
GOOGLE_PACKAGE_NAME = os.getenv("GOOGLE_PACKAGE_NAME")
GOOGLE_SUBSCRIPTION_ID = os.getenv("GOOGLE_SUBSCRIPTION_ID")
GOOGLE_BASEPLAN_ID = os.getenv("GOOGLE_BASEPLAN_ID")

# App Store
APPLE_ISSUER_ID = os.getenv("APPLE_ISSUER_ID")
APPLE_KEY_ID = os.getenv("APPLE_KEY_ID")
APPLE_PRIVATE_KEY = os.getenv("APPLE_PRIVATE_KEY")
APPLE_APP_ID = os.getenv("APPLE_APP_ID")
APPLE_SUBSCRIPTION_PRODUCT_ID = os.getenv("APPLE_SUBSCRIPTION_PRODUCT_ID")


@dataclass(frozen=True)
class InputData:
    country_prices: dict
    country_prices_usd: dict
    country_currencies: dict
    country_code_mapping: dict


def load_input_data() -> InputData:
    try:
        prices_df = pd.read_excel(EXCEL_FILE)
        country_prices = dict(zip(prices_df["Country"], prices_df["Smart_Price_Native"]))
        country_prices_usd = dict(zip(prices_df["Country"], prices_df["Smart_Price_USD"]))
        country_currencies = dict(zip(prices_df["Country"], prices_df["Currency_Code"]))
    except FileNotFoundError:
        raise SystemExit(f"❌ {EXCEL_FILE} not found. Run price_scaler.py first.")
    except Exception as e:
        raise SystemExit(f"❌ Failed to read {EXCEL_FILE}: {e}")

    try:
        with open("country_codes.json", "r") as f:
            country_code_mapping = json.load(f)
    except FileNotFoundError:
        raise SystemExit("❌ country_codes.json not found. Run update_country_codes.py first.")
    except Exception as e:
        raise SystemExit(f"❌ Failed to read country_codes.json: {e}")

    return InputData(
        country_prices=country_prices,
        country_prices_usd=country_prices_usd,
        country_currencies=country_currencies,
        country_code_mapping=country_code_mapping,
    )


def get_alpha_2_country_code(country_name: str, country_code_mapping: dict) -> "str | None":
    entry = country_code_mapping.get(country_name)
    return entry.get("alpha2") if entry else None


def get_alpha_3_country_code(country_name: str, country_code_mapping: dict) -> "str | None":
    entry = country_code_mapping.get(country_name)
    return entry.get("alpha3") if entry else None


def create_failure_report(failures: list) -> None:
    if not failures:
        print("✅ No failures to report - all countries updated successfully!")
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_filename = f"price_update_failures_{timestamp}.txt"

    google_failures = [f for f in failures if f["platform"] == "Google Play"]
    apple_failures = [f for f in failures if f["platform"] == "Apple App Store"]

    with open(report_filename, "w") as f:
        f.write("PRICE UPDATE FAILURE REPORT\n")
        f.write("=" * 50 + "\n")
        f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Total failures: {len(failures)}\n\n")

        if google_failures:
            f.write("GOOGLE PLAY FAILURES:\n")
            f.write("-" * 30 + "\n")
            for failure in google_failures:
                f.write(f"• {failure['country']} ({failure['country_code']}) - {failure['reason']}\n")
            f.write("\n")

        if apple_failures:
            f.write("APPLE APP STORE FAILURES:\n")
            f.write("-" * 30 + "\n")
            for failure in apple_failures:
                f.write(f"• {failure['country']} ({failure['country_code']}) - {failure['reason']}\n")
            f.write("\n")

    print(f"📄 Failure report saved to: {report_filename}")
    print(f"   Google Play failures: {len(google_failures)}")
    print(f"   Apple App Store failures: {len(apple_failures)}")
    print(f"   Total failures: {len(failures)}")


def _price_to_units_nanos(price: float) -> tuple:
    units = int(price)
    # round() handles float imprecision (e.g. 1.1 - 1 ≈ 0.0999... rounds to 0.10)
    nanos = int(round((price - units) * 100) * 1e7)
    return str(units), nanos


# --- GOOGLE PLAY ---

def _make_google_failure(country, country_code, price, currency, reason) -> dict:
    return {
        "country": country,
        "country_code": country_code,
        "price": price,
        "currency": currency,
        "reason": reason,
        "platform": "Google Play",
    }


def _update_google_region(subscription_update, country, country_code, currency_code, price) -> "dict | None":
    """Update a single region's price in subscription_update in-place.

    Returns a failure dict if the update could not be applied, None on success.
    """
    price_units, price_nanos = _price_to_units_nanos(float(price))

    for base_plan in subscription_update.get("basePlans", []):
        if base_plan.get("basePlanId") != GOOGLE_BASEPLAN_ID:
            continue

        for config in base_plan.get("regionalConfigs", []):
            if config.get("regionCode") != country_code:
                continue

            existing_currency = config.get("price", {}).get("currencyCode")
            if existing_currency != currency_code:
                reason = f"Currency mismatch: Google expects {existing_currency}, we have {currency_code}"
                print(f"   ⚠️  Skipping {country_code} - {reason}")
                return _make_google_failure(country, country_code, price, currency_code, reason)

            print(f"   Updating existing region config for {country_code} (currency matches: {currency_code})")
            config["price"] = {"currencyCode": currency_code, "units": price_units, "nanos": price_nanos}
            return None

        reason = "Region not found in Google Play configuration"
        print(f"   ⏭️  Skipping {country_code} - {reason}")
        return _make_google_failure(country, country_code, price, currency_code, reason)

    return _make_google_failure(
        country, country_code, price, currency_code,
        f"Base plan {GOOGLE_BASEPLAN_ID} not found in subscription",
    )


# These parse Google's human-readable 400 error strings to identify recoverable errors.
# If Google changes the wording the regex won't match, and the HttpError re-raises unchanged — safe fallback.
_CURRENCY_MISMATCH_RE = re.compile(r"region code (\w+).*?Expected (\w+) but got (\w+)")
_NOT_BILLABLE_RE = re.compile(r"Region code (\w+) is not billable")
_PRICE_OUT_OF_RANGE_RE = re.compile(r"([\w-]+): Price for (\w+) must be between [^\d]*([\d.]+)")


def _fix_currency_mismatch(subscription_update, region_code, expected_currency, got_currency) -> None:
    for bp in subscription_update.get("basePlans", []):
        for config in bp.get("regionalConfigs", []):
            if config.get("regionCode") == region_code:
                if config.get("price", {}).get("currencyCode") == got_currency:
                    config["price"]["currencyCode"] = expected_currency


def _fix_not_billable(subscription_update, region_code) -> None:
    for bp in subscription_update.get("basePlans", []):
        bp["regionalConfigs"] = [
            c for c in bp.get("regionalConfigs", [])
            if c.get("regionCode") != region_code
        ]


def _fix_price_out_of_range(subscription_update, base_plan_id, region_code, min_price_str) -> tuple:
    """Clamp region price to the API's minimum. Returns (min_price, failure_dict)."""
    try:
        min_price = float(min_price_str)
    except ValueError as e:
        raise RuntimeError(
            f"Could not parse minimum price '{min_price_str}' from API error"
        ) from e

    price_units, price_nanos = _price_to_units_nanos(min_price)
    for bp in subscription_update.get("basePlans", []):
        if bp.get("basePlanId") == base_plan_id:
            for config in bp.get("regionalConfigs", []):
                if config.get("regionCode") == region_code:
                    config["price"]["units"] = price_units
                    config["price"]["nanos"] = price_nanos

    failure = _make_google_failure(
        region_code, region_code, min_price, None,
        f"Price clamped to Google Play minimum ({min_price})",
    )
    return min_price, failure


def _handle_400_error(subscription_update, error_str, attempt, extra_failures, auto_fixed_regions) -> bool:
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

    price_range_match = _PRICE_OUT_OF_RANGE_RE.search(error_str)
    if price_range_match:
        base_plan_id, region_code, min_price_str = price_range_match.groups()
        detail = error_str.split("Details:")[0].strip()
        print(f"   Clamping {region_code} in {base_plan_id} to minimum price (attempt {attempt + 1}): {detail}")
        min_price, failure = _fix_price_out_of_range(subscription_update, base_plan_id, region_code, min_price_str)
        failure["reason"] = f"Price clamped to Google Play minimum ({min_price}): {detail}"
        extra_failures.append(failure)
        return True

    return False


def _patch_with_currency_fixes(service, subscription_update, regions_version, max_retries=10) -> tuple:
    """Submit the Play Console subscription patch, retrying on recoverable 400 errors.

    Returns (auto_fixed_regions, extra_failures).
    """
    auto_fixed_regions = set()
    extra_failures = []

    for attempt in range(max_retries):
        request = (
            service.monetization()
            .subscriptions()
            .patch(
                packageName=GOOGLE_PACKAGE_NAME,
                productId=GOOGLE_SUBSCRIPTION_ID,
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
            if not _handle_400_error(subscription_update, str(e), attempt, extra_failures, auto_fixed_regions):
                raise

    raise RuntimeError(f"Could not resolve all API errors after {max_retries} retries")


def _fetch_google_subscription(service) -> "tuple | None":
    """Fetch subscription from Play Console. Returns (subscription_response, regions_version) or None on error."""
    try:
        response = (
            service.monetization()
            .subscriptions()
            .get(packageName=GOOGLE_PACKAGE_NAME, productId=GOOGLE_SUBSCRIPTION_ID)
            .execute()
        )
        print(f"✅ Successfully retrieved subscription details")
        print(f"📋 Subscription has {len(response.get('basePlans', []))} base plans")
        for bp in response.get("basePlans", []):
            print(f"   - Base Plan: {bp.get('basePlanId')} with {len(bp.get('regionalConfigs', []))} regions")

        regions_version = response.get("regionsVersion", "2025/01")
        if regions_version == "1":
            regions_version = "2025/01"
        print(f"Current regions version: {regions_version}")
        return response, regions_version
    except Exception as e:
        print(f"❌ Error getting subscription details: {e}")
        return None


def update_google_play_prices(data: InputData) -> list:
    credentials = service_account.Credentials.from_service_account_file(
        GOOGLE_SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/androidpublisher"],
    )
    service = build("androidpublisher", "v3", credentials=credentials)

    result = _fetch_google_subscription(service)
    if result is None:
        return []
    subscription_response, regions_version = result
    subscription_update = copy.deepcopy(subscription_response)

    failures = []
    for country, price in data.country_prices.items():
        try:
            country_code = get_alpha_2_country_code(country, data.country_code_mapping)
            currency_code = data.country_currencies.get(country, "USD")
            print(f"Updating Google Play price for {country} ({country_code}): {price} {currency_code}")
            failure = _update_google_region(subscription_update, country, country_code, currency_code, price)
            if failure:
                failures.append(failure)
        except Exception as e:
            print(f"❌ Error updating {country}: {e}")
            failures.append(_make_google_failure(
                country,
                country_code if "country_code" in locals() else "Unknown",
                price,
                data.country_currencies.get(country, "Unknown"),
                f"Exception: {e}",
            ))

    auto_fixed, patch_failures = _patch_with_currency_fixes(service, subscription_update, regions_version)
    if auto_fixed:
        failures = [f for f in failures if f.get("country_code") not in auto_fixed]
    failures.extend(patch_failures)
    print(f"✅ Successfully updated Google Play prices")
    return failures


# --- APP STORE ---

def _build_price_payload(
    subscription_id: str, price_point_id: str, start_date: "str | None"
) -> dict:
    attributes: dict = {"preserveCurrentPrice": True}
    if start_date is not None:
        attributes["startDate"] = start_date
    return {
        "data": {
            "type": "subscriptionPrices",
            "attributes": attributes,
            "relationships": {
                "subscription": {"data": {"id": subscription_id, "type": "subscriptions"}},
                "subscriptionPricePoint": {
                    "data": {"id": price_point_id, "type": "subscriptionPricePoints"}
                },
            },
        }
    }


def get_apple_jwt():
    headers = {"alg": "ES256", "kid": APPLE_KEY_ID, "typ": "JWT"}
    payload = {
        "iss": APPLE_ISSUER_ID,
        "exp": int(time.time()) + 1200,
        "aud": "appstoreconnect-v1",
    }
    private_key = APPLE_PRIVATE_KEY.replace("\\n", "\n")
    return jwt.encode(payload, private_key, algorithm="ES256", headers=headers)


def get_apple_subscription_id(token):
    base_url = "https://api.appstoreconnect.apple.com/v1"
    headers = {"Authorization": f"Bearer {token}"}

    resp = requests.get(f"{base_url}/apps/{APPLE_APP_ID}/subscriptionGroups", headers=headers, timeout=30)
    if resp.status_code != 200:
        print(f"❌ Error fetching subscription groups for {APPLE_APP_ID}: {resp.text}")
        return None

    for group in resp.json()["data"]:
        resp2 = requests.get(
            f"{base_url}/subscriptionGroups/{group['id']}/subscriptions",
            headers=headers, timeout=30,
        )
        if resp2.status_code != 200:
            print(f"❌ Error fetching subscriptions for {APPLE_APP_ID}: {resp2.text}")
            return None
        for sub in resp2.json()["data"]:
            if sub["attributes"]["productId"] == APPLE_SUBSCRIPTION_PRODUCT_ID:
                return sub["id"]

    return None


def get_closest_price_point(token, subscription_id, country_code, target_price, expected_currency=None):
    """Get the closest available price point for a territory.

    Returns (closest_point, actual_currency) on success.
    Returns (None, actual_currency) when territory currency differs from expected_currency.
    Returns (None, None) when no price points or territory data are available.
    """
    base_url = "https://api.appstoreconnect.apple.com/v1"
    headers = {"Authorization": f"Bearer {token}"}

    url = (
        f"{base_url}/subscriptions/{subscription_id}/pricePoints"
        f"?include=territory&filter[territory]={country_code}&limit=1000"
    )
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code != 200:
        print(f"❌ Error fetching price points for {country_code}: {resp.text}")
        return None, None

    response_data = resp.json()
    price_points = response_data["data"]
    territories = response_data.get("included", [])

    print(f"✅ Found {len(price_points)} price points for {country_code}")
    print(f"✅ Found {len(territories)} territories")

    if not price_points or not territories:
        return None, None

    actual_currency = territories[0]["attributes"]["currency"]
    if expected_currency and actual_currency != expected_currency:
        return None, actual_currency

    closest_point = None
    min_difference = float("inf")
    for point in sorted(price_points, key=lambda p: float(p["attributes"]["customerPrice"])):
        difference = abs(float(point["attributes"]["customerPrice"]) - target_price)
        if difference < min_difference:
            min_difference = difference
            closest_point = point
        elif closest_point is not None:
            break

    return closest_point, actual_currency


def _make_apple_failure(country, country_code, price, currency, reason) -> dict:
    return {
        "country": country,
        "country_code": country_code,
        "price": price,
        "currency": currency,
        "reason": reason,
        "platform": "Apple App Store",
    }


def _resolve_apple_price_point(token, subscription_id, country, country_code, price, currency_code, country_prices_usd):
    """Find the best price point, retrying with USD when Apple uses a different currency.

    Returns (closest_point, final_price, final_currency, apple_territory_currency).
    apple_territory_currency is what Apple actually uses (needed for failure messages).
    """
    closest_point, apple_currency = get_closest_price_point(
        token, subscription_id, country_code, float(price), currency_code
    )

    if closest_point is None and apple_currency and apple_currency != currency_code:
        if apple_currency == "USD":
            usd_price = country_prices_usd.get(country)
            if usd_price:
                print(f"   Apple uses {apple_currency} for {country_code}, retrying with USD price {usd_price:.2f}")
                closest_point, _ = get_closest_price_point(
                    token, subscription_id, country_code, float(usd_price), apple_currency
                )
                if closest_point is not None:
                    return closest_point, usd_price, apple_currency, apple_currency
        else:
            print(f"   Apple uses {apple_currency} for {country_code} (we have {currency_code}) — skipping")

    return closest_point, price, currency_code, apple_currency


def _submit_apple_price(base_url, headers, subscription_id, price_point_id):
    """POST a price update, retrying without startDate if the territory has no existing price."""
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    resp = requests.post(
        f"{base_url}/subscriptionPrices",
        json=_build_price_payload(subscription_id, price_point_id, tomorrow),
        headers=headers,
        timeout=30,
    )
    if resp.status_code == 409 and "Create a starting price" in resp.text:
        resp = requests.post(
            f"{base_url}/subscriptionPrices",
            json=_build_price_payload(subscription_id, price_point_id, None),
            headers=headers,
            timeout=30,
        )
    return resp


def _update_apple_country(token, subscription_id, base_url, headers, country, price, data) -> tuple:
    """Process one country's price update. Returns (succeeded: bool, failure: dict | None)."""
    try:
        country_code = get_alpha_3_country_code(country, data.country_code_mapping)
    except Exception as e:
        failure_reason = f"Error getting country code: {e}"
        print(f"❌ {failure_reason}")
        return False, _make_apple_failure(country, "Unknown", price, data.country_currencies.get(country, "Unknown"), failure_reason)

    currency_code = data.country_currencies.get(country)
    if not currency_code:
        failure_reason = "Error getting currency code"
        print(f"❌ {failure_reason}")
        return False, _make_apple_failure(country, country_code, price, "Unknown", failure_reason)

    print(f"Updating App Store price for {country} ({country_code}): {price} {currency_code}")

    closest_point, final_price, final_currency, apple_currency = _resolve_apple_price_point(
        token, subscription_id, country, country_code, price, currency_code, data.country_prices_usd
    )

    if not closest_point:
        failure_reason = (
            f"No price points available with currency {currency_code}"
            if not apple_currency
            else f"No price points available (Apple uses {apple_currency}, we have {currency_code})"
        )
        print(f"⚠️ {failure_reason}")
        return False, _make_apple_failure(country, country_code, price, currency_code, failure_reason)

    closest_price = float(closest_point["attributes"]["customerPrice"])
    price_difference = abs(closest_price - float(final_price))
    print(f"   Found closest price point: {closest_price} {final_currency} (diff: {price_difference:.2f})")

    if price_difference > max(float(final_price) * 0.1, 2.0):
        failure_reason = f"Price difference too large: {price_difference:.2f} (max allowed: {max(float(final_price) * 0.1, 2.0):.2f})"
        print(f"⚠️ {failure_reason}")
        return False, _make_apple_failure(country, country_code, price, currency_code, failure_reason)

    resp = _submit_apple_price(base_url, headers, subscription_id, closest_point["id"])
    if resp.status_code == 201:
        print(f"✅ Successfully updated price for {country}")
        return True, None

    failure_reason = f"API Error {resp.status_code}: {resp.text}"
    print(f"❌ Failed to update {country}: {resp.status_code} - {resp.text}")
    return False, _make_apple_failure(country, country_code, price, currency_code, failure_reason)


def update_app_store_prices(data: InputData) -> list:
    """Apple App Store Connect pricing update using country-specific price point fetching."""
    print("🍎 Apple App Store Connect Pricing Update")
    print("=" * 50)

    token = get_apple_jwt()
    base_url = "https://api.appstoreconnect.apple.com/v1"
    headers = {"Authorization": f"Bearer {token}"}

    subscription_id = get_apple_subscription_id(token)
    if not subscription_id:
        print(f"❌ Could not find subscription for app {APPLE_APP_ID}. Check APPLE_SUBSCRIPTION_PRODUCT_ID.")
        return []

    print(f"\n🔄 Updating subscription prices for {len(data.country_prices)} countries...")
    print("-" * 60)

    updated_count = 0
    not_found_count = 0
    api_error_count = 0
    failures = []

    for country, price in data.country_prices.items():
        succeeded, failure = _update_apple_country(token, subscription_id, base_url, headers, country, price, data)
        if succeeded:
            updated_count += 1
        elif failure:
            failures.append(failure)
            if "No price points" in failure["reason"] or "Price difference" in failure["reason"]:
                not_found_count += 1
            else:
                api_error_count += 1

    print(f"\n📊 Apple App Store Update Summary:")
    print(f"✅ Successfully updated: {updated_count} countries")
    print(f"⚠️ Skipped (API errors): {api_error_count} countries")
    print(f"⚠️ No price point found: {not_found_count} countries")

    if not_found_count > 0:
        print(f"\n💡 Note: {not_found_count} countries don't have suitable price points.")
        print("   You may need to create additional price points in App Store Connect")
        print("   or adjust your pricing to match available price points.")

    return failures


def _validate_env() -> bool:
    """Validate required environment variables. Prints error and returns False if any are missing."""
    checks = [
        (GOOGLE_SERVICE_ACCOUNT_FILE, "GOOGLE_SERVICE_ACCOUNT_FILE not set in .env file", None),
        (GOOGLE_PACKAGE_NAME, "GOOGLE_PACKAGE_NAME not set in .env file", None),
        (GOOGLE_SUBSCRIPTION_ID and GOOGLE_SUBSCRIPTION_ID != "your_subscription_id",
         "GOOGLE_SUBSCRIPTION_ID not properly configured in .env file",
         "Please set your actual subscription ID from Google Play Console"),
        (GOOGLE_BASEPLAN_ID and GOOGLE_BASEPLAN_ID != "base-plan",
         "GOOGLE_BASEPLAN_ID not properly configured in .env file",
         "Please set your actual base plan ID from Google Play Console"),
        (APPLE_ISSUER_ID and APPLE_ISSUER_ID != "your_issuer_id",
         "APPLE_ISSUER_ID not properly configured in .env file",
         "Please set your Apple App Store Connect issuer ID"),
        (APPLE_KEY_ID and APPLE_KEY_ID != "your_key_id",
         "APPLE_KEY_ID not properly configured in .env file",
         "Please set your Apple App Store Connect key ID"),
        (APPLE_PRIVATE_KEY and "YOUR_PRIVATE_KEY_CONTENT_HERE" not in APPLE_PRIVATE_KEY,
         "APPLE_PRIVATE_KEY not properly configured in .env file",
         "Please set your Apple App Store Connect private key"),
        (APPLE_APP_ID and APPLE_APP_ID != "your_app_id",
         "APPLE_APP_ID not properly configured in .env file",
         "Please set your Apple App Store Connect app ID"),
        (APPLE_SUBSCRIPTION_PRODUCT_ID, "APPLE_SUBSCRIPTION_PRODUCT_ID not set in .env file", None),
    ]
    for ok, error_msg, hint in checks:
        if not ok:
            print(f"❌ Error: {error_msg}")
            if hint:
                print(hint)
            return False
    return True


def main():
    data = load_input_data()

    print("🚀 Starting Subscription Price Applier...")
    print(f"📊 Loaded {len(data.country_prices)} countries from {EXCEL_FILE}")
    print(f"💰 Loaded {len(data.country_currencies)} currency codes from {EXCEL_FILE}")

    if not _validate_env():
        return

    print("✅ Environment variables validated")
    print(f"📱 Package: {GOOGLE_PACKAGE_NAME}")
    print(f"🆔 Subscription: {GOOGLE_SUBSCRIPTION_ID}")
    print(f"📋 Base Plan: {GOOGLE_BASEPLAN_ID}")
    print(f"🍎 Apple App ID: {APPLE_APP_ID}")
    print(f"🍎 Apple Issuer ID: {APPLE_ISSUER_ID}")
    print(f"🍎 Apple Key ID: {APPLE_KEY_ID}")
    print()

    print("🔄 Updating Google Play prices...")
    google_failures = update_google_play_prices(data)

    print("🔄 Updating App Store prices...")
    apple_failures = update_app_store_prices(data)

    print("\n📋 Creating failure report...")
    create_failure_report(google_failures + apple_failures)

    print("✅ Price update process completed!")


if __name__ == "__main__":
    main()
