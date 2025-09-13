import os
import json
import pandas as pd
import requests
import time
import jwt
from datetime import datetime
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

# --- CONFIG ---
EXCEL_FILE = "price_scaled.xlsx"

# Load env variables
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

# --- READ PRICES ---
prices_df = pd.read_excel(EXCEL_FILE)
country_prices = dict(zip(prices_df["Country"], prices_df["Smart_Price_Native"]))
country_currencies = dict(zip(prices_df["Country"], prices_df["Currency_Code"]))

# Load country code mapping from JSON file
with open("country_codes.json", "r") as f:
    COUNTRY_CODE_MAPPING = json.load(f)

# Global list to track failures
FAILURES = []


def get_alpha_2_country_code(country_name):
    """Convert country name to ISO 3166-1 alpha-2 code"""
    return COUNTRY_CODE_MAPPING.get(country_name).get("alpha2")


def get_alpha_3_country_code(country_name):
    """Convert country name to ISO 3166-1 alpha-3 code"""
    return COUNTRY_CODE_MAPPING.get(country_name).get("alpha3")


def create_failure_report():
    """Create a simple failure report file"""
    if not FAILURES:
        print("✅ No failures to report - all countries updated successfully!")
        return

    # Create failure report filename with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_filename = f"price_update_failures_{timestamp}.txt"

    with open(report_filename, "w") as f:
        f.write("PRICE UPDATE FAILURE REPORT\n")
        f.write("=" * 50 + "\n")
        f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Total failures: {len(FAILURES)}\n\n")

        # Group failures by platform
        google_failures = [f for f in FAILURES if f["platform"] == "Google Play"]
        apple_failures = [f for f in FAILURES if f["platform"] == "Apple App Store"]

        if google_failures:
            f.write("GOOGLE PLAY FAILURES:\n")
            f.write("-" * 30 + "\n")
            for failure in google_failures:
                f.write(
                    f"• {failure['country']} ({failure['country_code']}) - {failure['reason']}\n"
                )
            f.write("\n")

        if apple_failures:
            f.write("APPLE APP STORE FAILURES:\n")
            f.write("-" * 30 + "\n")
            for failure in apple_failures:
                f.write(
                    f"• {failure['country']} ({failure['country_code']}) - {failure['reason']}\n"
                )
            f.write("\n")

    print(f"📄 Failure report saved to: {report_filename}")
    print(f"   Google Play failures: {len(google_failures)}")
    print(f"   Apple App Store failures: {len(apple_failures)}")
    print(f"   Total failures: {len(FAILURES)}")


# --- GOOGLE PLAY ---
def update_google_play_prices():
    credentials = service_account.Credentials.from_service_account_file(
        GOOGLE_SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/androidpublisher"],
    )
    service = build("androidpublisher", "v3", credentials=credentials)

    try:
        subscription_request = (
            service.monetization()
            .subscriptions()
            .get(packageName=GOOGLE_PACKAGE_NAME, productId=GOOGLE_SUBSCRIPTION_ID)
        )
        subscription_response = subscription_request.execute()
        print(f"✅ Successfully retrieved subscription details")

        print(
            f"📋 Subscription has {len(subscription_response.get('basePlans', []))} base plans"
        )
        for bp in subscription_response.get("basePlans", []):
            print(
                f"   - Base Plan: {bp.get('basePlanId')} with {len(bp.get('regionalConfigs', []))} regions"
            )

        regions_version = subscription_response.get("regionsVersion", "2025/01")
        print(f"Current regions version: {regions_version}")

        if regions_version == "1":
            regions_version = "2025/01"
            print(f"Updated to latest regions version: {regions_version}")

    except Exception as e:
        print(f"❌ Error getting subscription details: {e}")
        return

    subscription_update = subscription_response.copy()

    for country, price in country_prices.items():
        try:
            country_code = get_alpha_2_country_code(country)

            currency_code = country_currencies.get(country, "USD")
            print(
                f"Updating Google Play price for {country} ({country_code}): {price} {currency_code}"
            )

            price_units = int(price)
            price_nanos = int(round((price - price_units) * 100) * 1e7)

            base_plans = subscription_update.get("basePlans", [])
            price_updated = False

            for base_plan in base_plans:
                if base_plan.get("basePlanId") == GOOGLE_BASEPLAN_ID:
                    regional_configs = base_plan.get("regionalConfigs", [])
                    region_found = False
                    for config in regional_configs:
                        if config.get("regionCode") == country_code:
                            region_found = True
                            existing_currency = config.get("price", {}).get(
                                "currencyCode"
                            )

                            if existing_currency == currency_code:
                                print(
                                    f"   Updating existing region config for {country_code} (currency matches: {currency_code})"
                                )
                                config["price"] = {
                                    "currencyCode": currency_code,
                                    "units": str(price_units),
                                    "nanos": price_nanos,
                                }
                                price_updated = True
                            else:
                                failure_reason = f"Currency mismatch: Google expects {existing_currency}, we have {currency_code}"
                                print(
                                    f"   ⚠️  Skipping {country_code} - {failure_reason}"
                                )
                                FAILURES.append(
                                    {
                                        "country": country,
                                        "country_code": country_code,
                                        "price": price,
                                        "currency": currency_code,
                                        "reason": failure_reason,
                                        "platform": "Google Play",
                                    }
                                )
                            break

                    if not region_found:
                        failure_reason = "Region not found in Google Play configuration"
                        print(f"   ⏭️  Skipping {country_code} - {failure_reason}")
                        FAILURES.append(
                            {
                                "country": country,
                                "country_code": country_code,
                                "price": price,
                                "currency": currency_code,
                                "reason": failure_reason,
                                "platform": "Google Play",
                            }
                        )
                        continue

                    if not price_updated:
                        continue

                    base_plan["regionalConfigs"] = regional_configs
                    break
        except Exception as e:
            error_msg = f"Exception: {str(e)}"
            print(f"❌ Error updating {country}: {e}")
            FAILURES.append(
                {
                    "country": country,
                    "country_code": (
                        country_code if "country_code" in locals() else "Unknown"
                    ),
                    "price": price,
                    "currency": country_currencies.get(country, "Unknown"),
                    "reason": error_msg,
                    "platform": "Google Play",
                }
            )
            continue

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
    regions_version_param = f"regionsVersion.version={regions_version}"
    if "?" in request.uri:
        request.uri += f"&{regions_version_param}"
    else:
        request.uri += f"?{regions_version_param}"

    response = request.execute()
    print(f"✅ Successfully updated Google Play prices")


# --- APP STORE ---
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

    url = f"{base_url}/apps/{APPLE_APP_ID}/subscriptionGroups"
    resp = requests.get(url, headers=headers)
    if resp.status_code != 200:
        print(f"❌ Error fetching subscription groups for {APPLE_APP_ID}: {resp.text}")
        return None

    response_data = resp.json()
    subscription_groups = response_data["data"]

    for subscription_group in subscription_groups:
        url = f"{base_url}/subscriptionGroups/{subscription_group['id']}/subscriptions"
        resp = requests.get(url, headers=headers)
        if resp.status_code != 200:
            print(f"❌ Error fetching subscriptions for {APPLE_APP_ID}: {resp.text}")
            return None

        response_data = resp.json()
        subscriptions = response_data["data"]
        for subscription in subscriptions:
            if subscription["attributes"]["productId"] == APPLE_SUBSCRIPTION_PRODUCT_ID:
                return subscription["id"]

    return None


def get_closest_price_point(
    token, subscription_id, country_code, target_price, expected_currency
):
    """
    Get the closest available price point for a specific country and currency.
    Only returns price points that match the expected currency.
    """
    base_url = "https://api.appstoreconnect.apple.com/v1"
    headers = {"Authorization": f"Bearer {token}"}

    url = f"{base_url}/subscriptions/{subscription_id}/pricePoints?include=territory&filter[territory]={country_code}&limit=1000"
    resp = requests.get(url, headers=headers)
    if resp.status_code != 200:
        print(f"❌ Error fetching price points for {country_code}: {resp.text}")
        return None

    response_data = resp.json()
    price_points = response_data["data"]
    territories = response_data.get("included", [])

    print(f"✅ Found {len(price_points)} price points for {country_code}")
    print(f"✅ Found {len(territories)} territories")

    if not price_points:
        return None

    if not territories:
        return None

    territory = territories[0]
    currency = territory["attributes"]["currency"]

    if currency != expected_currency:
        return None

    closest_point = None
    min_difference = float("inf")

    for point in price_points:
        point_price = float(point["attributes"]["customerPrice"])
        difference = abs(point_price - target_price)

        if difference < min_difference:
            min_difference = difference
            closest_point = point
        else:
            # Since price_points are sorted ascending, once difference increases, we can break
            break

    return closest_point


def update_app_store_prices():
    """
    Apple App Store Connect pricing update using country-specific price point fetching.
    """
    print("🍎 Apple App Store Connect Pricing Update")
    print("=" * 50)

    token = get_apple_jwt()
    base_url = "https://api.appstoreconnect.apple.com/v1"
    headers = {"Authorization": f"Bearer {token}"}

    subscription_id = get_apple_subscription_id(token)
    if not subscription_id:
        print(f"❌ Error fetching subscription ID for {APPLE_APP_ID}: {resp.text}")
        return None

    updated_count = 0
    skipped_count = 0
    not_found_count = 0

    print(f"\n🔄 Updating subscription prices for {len(country_prices)} countries...")
    print("-" * 60)

    for country, price in country_prices.items():
        try:
            country_code = get_alpha_3_country_code(country)
        except Exception as e:
            failure_reason = f"Error getting country code: {e}"
            print(f"❌ {failure_reason}")
            FAILURES.append(
                {
                    "country": country,
                    "country_code": "Unknown",
                    "price": price,
                    "currency": country_currencies.get(country, "Unknown"),
                    "reason": failure_reason,
                    "platform": "Apple App Store",
                }
            )
            continue

        currency_code = country_currencies.get(country)

        if not currency_code:
            failure_reason = "Error getting currency code"
            print(f"❌ {failure_reason}")
            FAILURES.append(
                {
                    "country": country,
                    "country_code": country_code,
                    "price": price,
                    "currency": "Unknown",
                    "reason": failure_reason,
                    "platform": "Apple App Store",
                }
            )
            continue

        print(
            f"Updating App Store price for {country} ({country_code}): {price} {currency_code}"
        )

        # Get the closest price point for this country with currency matching
        closest_point = get_closest_price_point(
            token, subscription_id, country_code, float(price), currency_code
        )

        if not closest_point:
            failure_reason = f"No price points available with currency {currency_code}"
            print(f"⚠️ {failure_reason}")
            FAILURES.append(
                {
                    "country": country,
                    "country_code": country_code,
                    "price": price,
                    "currency": currency_code,
                    "reason": failure_reason,
                    "platform": "Apple App Store",
                }
            )
            not_found_count += 1
            continue

        closest_price = float(closest_point["attributes"]["customerPrice"])
        price_difference = abs(closest_price - float(price))

        print(
            f"   Found closest price point: {closest_price} {currency_code} (diff: {price_difference:.2f})"
        )

        # Only proceed if the price difference is reasonable (within 10% or $2)
        if price_difference > max(float(price) * 0.1, 2.0):
            failure_reason = f"Price difference too large: {price_difference:.2f} (max allowed: {max(float(price) * 0.1, 2.0):.2f})"
            print(f"⚠️ {failure_reason}")
            FAILURES.append(
                {
                    "country": country,
                    "country_code": country_code,
                    "price": price,
                    "currency": currency_code,
                    "reason": failure_reason,
                    "platform": "Apple App Store",
                }
            )
            not_found_count += 1
            continue

        data = {
            "data": {
                "type": "subscriptionPrices",
                "attributes": {
                    "startDate": time.strftime("%Y-%m-%d"),
                    "preserveCurrentPrice": True,
                },
                "relationships": {
                    "subscription": {
                        "data": {"id": subscription_id, "type": "subscriptions"}
                    },
                    "subscriptionPricePoint": {
                        "data": {
                            "id": closest_point["id"],
                            "type": "subscriptionPricePoints",
                        }
                    },
                },
            }
        }

        resp = requests.post(
            f"{base_url}/subscriptionPrices", json=data, headers=headers
        )

        if resp.status_code == 201:
            print(f"✅ Successfully updated price for {country}")
            updated_count += 1
        else:
            failure_reason = f"API Error {resp.status_code}: {resp.text}"
            print(f"❌ Failed to update {country}: {resp.status_code} - {resp.text}")
            FAILURES.append(
                {
                    "country": country,
                    "country_code": country_code,
                    "price": price,
                    "currency": currency_code,
                    "reason": failure_reason,
                    "platform": "Apple App Store",
                }
            )
            skipped_count += 1

    print(f"\n📊 Apple App Store Update Summary:")
    print(f"✅ Successfully updated: {updated_count} countries")
    print(f"⚠️ Skipped (API errors): {skipped_count} countries")
    print(f"⚠️ No price point found: {not_found_count} countries")

    if not_found_count > 0:
        print(
            f"\n💡 Note: {not_found_count} countries don't have suitable price points."
        )
        print("   You may need to create additional price points in App Store Connect")
        print("   or adjust your pricing to match available price points.")


def main():
    print("🚀 Starting Subscription Price Applier...")
    print(f"📊 Loaded {len(country_prices)} countries from {EXCEL_FILE}")
    print(f"💰 Loaded {len(country_currencies)} currency codes from {EXCEL_FILE}")

    # Validate environment variables
    if not GOOGLE_SERVICE_ACCOUNT_FILE:
        print("❌ Error: GOOGLE_SERVICE_ACCOUNT_FILE not set in .env file")
        return

    if not GOOGLE_PACKAGE_NAME:
        print("❌ Error: GOOGLE_PACKAGE_NAME not set in .env file")
        return

    if not GOOGLE_SUBSCRIPTION_ID or GOOGLE_SUBSCRIPTION_ID == "your_subscription_id":
        print("❌ Error: GOOGLE_SUBSCRIPTION_ID not properly configured in .env file")
        print("Please set your actual subscription ID from Google Play Console")
        return

    if not GOOGLE_BASEPLAN_ID or GOOGLE_BASEPLAN_ID == "base-plan":
        print("❌ Error: GOOGLE_BASEPLAN_ID not properly configured in .env file")
        print("Please set your actual base plan ID from Google Play Console")
        return

    # Validate Apple App Store environment variables
    if not APPLE_ISSUER_ID or APPLE_ISSUER_ID == "your_issuer_id":
        print("❌ Error: APPLE_ISSUER_ID not properly configured in .env file")
        print("Please set your Apple App Store Connect issuer ID")
        return

    if not APPLE_KEY_ID or APPLE_KEY_ID == "your_key_id":
        print("❌ Error: APPLE_KEY_ID not properly configured in .env file")
        print("Please set your Apple App Store Connect key ID")
        return

    if not APPLE_PRIVATE_KEY or "YOUR_PRIVATE_KEY_CONTENT_HERE" in APPLE_PRIVATE_KEY:
        print("❌ Error: APPLE_PRIVATE_KEY not properly configured in .env file")
        print("Please set your Apple App Store Connect private key")
        return

    if not APPLE_APP_ID or APPLE_APP_ID == "your_app_id":
        print("❌ Error: APPLE_APP_ID not properly configured in .env file")
        print("Please set your Apple App Store Connect app ID")
        return

    print("✅ Environment variables validated")
    print(f"📱 Package: {GOOGLE_PACKAGE_NAME}")
    print(f"🆔 Subscription: {GOOGLE_SUBSCRIPTION_ID}")
    print(f"📋 Base Plan: {GOOGLE_BASEPLAN_ID}")
    print(f"🍎 Apple App ID: {APPLE_APP_ID}")
    print(f"🍎 Apple Issuer ID: {APPLE_ISSUER_ID}")
    print(f"🍎 Apple Key ID: {APPLE_KEY_ID}")
    print()

    # Update Google Play prices
    print("🔄 Updating Google Play prices...")
    update_google_play_prices()

    # Update App Store prices
    print("🔄 Updating App Store prices...")
    update_app_store_prices()

    # Create failure report
    print("\n📋 Creating failure report...")
    create_failure_report()

    print("✅ Price update process completed!")


if __name__ == "__main__":
    main()
