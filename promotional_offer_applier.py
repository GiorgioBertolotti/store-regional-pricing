#!/usr/bin/env python3
"""
Promotional Offer Applier - creates a time-limited discounted offer on top of the
already-scaled base prices (price_scaled.xlsx), on both Google Play and App Store Connect.

Google Play: creates a monetization.subscriptions.basePlans.offers resource with a
relativeDiscount phase (percentage off whatever the base plan charges in each region),
then activates it.

App Store Connect: creates a subscriptionPromotionalOffers resource. Apple has no
"percentage off" concept - it needs an absolute price per territory, so for each country
we take the current Smart_Price_Native from price_scaled.xlsx, apply the discount, and
resolve it to the closest available subscriptionPricePoint, retrying in USD when Apple
prices that territory in USD (same logic as the base price applier). Apple's PATCH
endpoint for this resource rejects the same payload shape that its POST endpoint
accepts (undocumented), so re-running with the same offer code deletes and fully
recreates the offer via POST rather than trying to extend it in place - at the cost of
a brief window where the offer doesn't exist. Apple also never frees up a name/offerCode
once used, even after deletion (also undocumented), so the recreate auto-retries with a
timestamp-suffixed name/offerCode on that specific collision rather than failing outright.

NOTE: Apple's subscriptionPromotionalOffers endpoint is thinly documented and its runtime
validation isn't reflected in Apple's own published OpenAPI schema. Per-territory prices
are set via a JSON:API compound document: the "prices" relationship references client-chosen
ids wrapped as "${local-id}" (Apple's own 409 error dictates this exact format - a plain
id like "NOR" is rejected with ENTITY_ERROR.INCLUDED.INVALID_ID), and a top-level "included"
array supplies, for each of those ids, a subscriptionPricePoint + territory relationship.
If Apple changes this format the Apple half will fail loudly with the raw API error, while
the Google half is unaffected.
"""

import time
from concurrent.futures import ThreadPoolExecutor

from googleapiclient.errors import HttpError

from subscription_price_applier import (
    APPLE_APP_ID,
    APPLE_MAX_WORKERS,
    GOOGLE_BASEPLAN_ID,
    GOOGLE_PACKAGE_NAME,
    GOOGLE_SERVICE_ACCOUNT_FILE,
    GOOGLE_SUBSCRIPTION_ID,
    InputData,
    _fetch_google_subscription,
    _make_apple_failure,
    _NOT_BILLABLE_RE,
    _resolve_apple_price_point,
    _validate_env,
    create_failure_report,
    get_alpha_2_country_code,
    get_alpha_3_country_code,
    get_apple_jwt,
    get_apple_subscription_id,
    load_input_data,
)
from google.oauth2 import service_account
from googleapiclient.discovery import build
import requests

# duration choice -> (Apple SubscriptionOfferDuration enum, Google ISO 8601 period)
DURATION_CHOICES = {
    "1": ("ONE_WEEK", "P1W"),
    "2": ("ONE_MONTH", "P1M"),
    "3": ("TWO_MONTHS", "P2M"),
    "4": ("THREE_MONTHS", "P3M"),
    "5": ("SIX_MONTHS", "P6M"),
    "6": ("ONE_YEAR", "P1Y"),
}

PLATFORM_CHOICES = {
    "1": {"apple", "google"},
    "2": {"apple"},
    "3": {"google"},
}


def prompt_offer_config() -> dict:
    print("📋 Promotional offer configuration")
    print("-" * 40)

    print("Which platform(s) do you want to create this offer on?")
    print("  1. Both (Apple + Google)")
    print("  2. Apple only")
    print("  3. Google only")
    platform_choice = input("Choice: ").strip()
    if platform_choice not in PLATFORM_CHOICES:
        raise SystemExit("❌ Invalid platform choice")
    platforms = PLATFORM_CHOICES[platform_choice]

    try:
        discount_percent = float(input("\nDiscount percentage off the current price (e.g. 30): "))
    except ValueError:
        raise SystemExit("❌ Invalid discount percentage - please enter a number")
    if not 0 < discount_percent < 100:
        raise SystemExit("❌ Discount percentage must be between 0 and 100")

    try:
        num_periods = int(input("Number of billing cycles the discount applies to: "))
    except ValueError:
        raise SystemExit("❌ Invalid number of billing cycles - please enter a whole number")
    if num_periods < 1:
        raise SystemExit("❌ Number of billing cycles must be at least 1")

    print("\nBilling period of the subscription this offer applies to:")
    for key, (apple_duration, _) in DURATION_CHOICES.items():
        print(f"  {key}. {apple_duration}")
    duration_choice = input("Choice: ").strip()
    if duration_choice not in DURATION_CHOICES:
        raise SystemExit("❌ Invalid billing period choice")
    apple_duration, google_duration = DURATION_CHOICES[duration_choice]

    offer_name = offer_code = None
    if "apple" in platforms:
        offer_name = input("\nOffer reference name (Apple, human-readable, e.g. 'Summer sale 30% off'): ").strip()
        offer_code = input("Offer code (Apple, unique alphanumeric identifier, e.g. SUMMER30): ").strip()

    google_offer_id = None
    if "google" in platforms:
        google_offer_id = input(
            "\nGoogle Play offer id (lowercase letters/numbers/hyphens, e.g. summer-30-off): "
        ).strip()

    return {
        "platforms": platforms,
        "discount_percent": discount_percent,
        "num_periods": num_periods,
        "apple_duration": apple_duration,
        "google_duration": google_duration,
        "offer_name": offer_name,
        "offer_code": offer_code,
        "google_offer_id": google_offer_id,
    }


# --- GOOGLE PLAY ---

def _remove_region(offer_body: dict, region_code: str) -> None:
    offer_body["regionalConfigs"] = [
        c for c in offer_body["regionalConfigs"] if c["regionCode"] != region_code
    ]
    for phase in offer_body["phases"]:
        phase["regionalConfigs"] = [
            c for c in phase["regionalConfigs"] if c["regionCode"] != region_code
        ]


def _create_offer_with_retries(service, offer_body: dict, regions_version, max_retries: int = 50) -> list:
    """Create the offer, dropping regions Google reports as not billable and retrying.

    Returns the list of region codes that were dropped.
    """
    removed_regions = []
    for attempt in range(max_retries):
        request = (
            service.monetization()
            .subscriptions()
            .basePlans()
            .offers()
            .create(
                packageName=GOOGLE_PACKAGE_NAME,
                productId=GOOGLE_SUBSCRIPTION_ID,
                basePlanId=GOOGLE_BASEPLAN_ID,
                offerId=offer_body["offerId"],
                body=offer_body,
            )
        )
        sep = "&" if "?" in request.uri else "?"
        request.uri += f"{sep}regionsVersion.version={regions_version}"

        try:
            request.execute()
            return removed_regions
        except HttpError as e:
            if e.resp.status != 400:
                raise
            match = _NOT_BILLABLE_RE.search(str(e))
            if not match:
                raise
            region_code = match.group(1)
            print(f"   Removing non-billable region {region_code} (attempt {attempt + 1})")
            _remove_region(offer_body, region_code)
            removed_regions.append(region_code)

    raise RuntimeError(f"Could not resolve all API errors after {max_retries} retries")


def create_google_offer(data: InputData, config: dict) -> list:
    print("\n🤖 Google Play Promotional Offer")
    print("=" * 50)

    credentials = service_account.Credentials.from_service_account_file(
        GOOGLE_SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/androidpublisher"],
    )
    service = build("androidpublisher", "v3", credentials=credentials)

    result = _fetch_google_subscription(service)
    if result is None:
        return [{"platform": "Google Play", "reason": "Could not fetch subscription", "country": "N/A", "country_code": "N/A", "price": None, "currency": None}]
    _, regions_version = result

    region_codes = sorted({
        get_alpha_2_country_code(country, data.country_code_mapping)
        for country in data.country_prices
        if get_alpha_2_country_code(country, data.country_code_mapping)
    })

    offer_body = {
        "packageName": GOOGLE_PACKAGE_NAME,
        "productId": GOOGLE_SUBSCRIPTION_ID,
        "basePlanId": GOOGLE_BASEPLAN_ID,
        "offerId": config["google_offer_id"],
        "regionalConfigs": [{"regionCode": cc, "newSubscriberAvailability": True} for cc in region_codes],
        "phases": [
            {
                "recurrenceCount": config["num_periods"],
                "duration": config["google_duration"],
                "regionalConfigs": [
                    {"regionCode": cc, "relativeDiscount": config["discount_percent"] / 100}
                    for cc in region_codes
                ],
            }
        ],
    }

    failures = []
    try:
        removed_regions = _create_offer_with_retries(service, offer_body, regions_version)
        kept = len(region_codes) - len(removed_regions)
        print(f"✅ Created offer '{config['google_offer_id']}' in DRAFT state for {kept} regions")
        if removed_regions:
            print(f"   Excluded {len(removed_regions)} non-billable regions: {', '.join(removed_regions)}")
    except (HttpError, RuntimeError) as e:
        reason = f"Failed to create offer: {e}"
        print(f"❌ {reason}")
        failures.append({
            "platform": "Google Play", "country": "N/A", "country_code": "N/A",
            "price": None, "currency": None, "reason": reason,
        })
        return failures

    try:
        service.monetization().subscriptions().basePlans().offers().activate(
            packageName=GOOGLE_PACKAGE_NAME,
            productId=GOOGLE_SUBSCRIPTION_ID,
            basePlanId=GOOGLE_BASEPLAN_ID,
            offerId=config["google_offer_id"],
        ).execute()
        print(f"✅ Activated offer '{config['google_offer_id']}'")
    except HttpError as e:
        reason = f"Offer created but activation failed: {e}"
        print(f"⚠️ {reason}")
        failures.append({
            "platform": "Google Play", "country": "N/A", "country_code": "N/A",
            "price": None, "currency": None, "reason": reason,
        })

    return failures


# --- APP STORE ---

def _find_apple_offer_id(base_url: str, headers: dict, subscription_id: str, offer_code: str) -> "str | None":
    """Look up an existing promotional offer on this subscription by its offerCode."""
    url = f"{base_url}/subscriptions/{subscription_id}/promotionalOffers?limit=200"
    while url:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        for offer in body.get("data", []):
            if offer.get("attributes", {}).get("offerCode") == offer_code:
                return offer["id"]
        url = body.get("links", {}).get("next")
    return None


def create_apple_offer(data: InputData, config: dict) -> list:
    print("\n🍎 Apple App Store Promotional Offer")
    print("=" * 50)

    token = get_apple_jwt()
    base_url = "https://api.appstoreconnect.apple.com/v1"
    headers = {"Authorization": f"Bearer {token}"}

    subscription_id = get_apple_subscription_id(token)
    if not subscription_id:
        reason = f"Could not find subscription for app {APPLE_APP_ID}"
        print(f"❌ {reason}")
        return [_make_apple_failure("N/A", "N/A", None, None, reason)]

    # Apple's PATCH endpoint for this resource rejects the same inline-creation payload
    # that POST accepts (undocumented, reported to Apple-side validation, not our schema).
    # POST is the only path proven to work, so re-running with the same offer code deletes
    # and fully recreates the offer instead of trying to extend it in place.
    try:
        existing_offer_id = _find_apple_offer_id(base_url, headers, subscription_id, config["offer_code"])
    except requests.RequestException as e:
        reason = f"Could not check for an existing offer with this code: {e}"
        print(f"❌ {reason}")
        return [_make_apple_failure("N/A", "N/A", None, None, reason)]

    if existing_offer_id:
        print(f"ℹ️  Offer code '{config['offer_code']}' already exists - deleting it to recreate from scratch")
        del_resp = requests.delete(f"{base_url}/subscriptionPromotionalOffers/{existing_offer_id}", headers=headers, timeout=30)
        if del_resp.status_code != 204:
            reason = f"Could not delete existing offer before recreating it: API Error {del_resp.status_code}: {del_resp.text}"
            print(f"❌ {reason}")
            return [_make_apple_failure("N/A", "N/A", None, None, reason)]

    price_refs = []
    included = []
    failures = []
    discount_factor = 1 - config["discount_percent"] / 100
    discounted_usd_prices = {c: v * discount_factor for c, v in data.country_prices_usd.items()}

    resolvable = []
    for country, price in data.country_prices.items():
        country_code = get_alpha_3_country_code(country, data.country_code_mapping)
        currency_code = data.country_currencies.get(country)
        if not country_code or not currency_code:
            failures.append(_make_apple_failure(country, country_code or "Unknown", price, currency_code, "Missing country/currency code mapping"))
            continue
        resolvable.append((country, country_code, currency_code, float(price) * discount_factor))

    def _resolve_one(item):
        country, country_code, currency_code, target_price = item
        closest_point, _, _, apple_currency = _resolve_apple_price_point(
            token, subscription_id, country, country_code, target_price, currency_code, discounted_usd_prices
        )
        return country, country_code, currency_code, target_price, closest_point, apple_currency

    # Price-point resolution is one HTTP round-trip per country (I/O-bound), so running
    # several in parallel is safe and cuts wall-clock time significantly.
    with ThreadPoolExecutor(max_workers=APPLE_MAX_WORKERS) as executor:
        results = executor.map(_resolve_one, resolvable)

        for country, country_code, currency_code, target_price, closest_point, apple_currency in results:
            print(f"Resolving promo price point for {country} ({country_code}): target {target_price:.2f} {currency_code}")
            if not closest_point:
                reason = (
                    f"No price points available with currency {currency_code}"
                    if not apple_currency
                    else f"No price points available (Apple uses {apple_currency}, we have {currency_code})"
                )
                print(f"⚠️ {reason}")
                failures.append(_make_apple_failure(country, country_code, target_price, currency_code, reason))
                continue

            # Apple requires included-entity ids to be wrapped as "${local-id}" (a client-scoped
            # id for this request only, not a real resource id). country_code (alpha-3) is
            # unique per territory, so it doubles as the local id linking this relationship
            # entry to its "included" definition below.
            local_id = f"${{{country_code}}}"
            price_refs.append({"type": "subscriptionPromotionalOfferPrices", "id": local_id})
            included.append({
                "type": "subscriptionPromotionalOfferPrices",
                "id": local_id,
                "relationships": {
                    "subscriptionPricePoint": {"data": {"type": "subscriptionPricePoints", "id": closest_point["id"]}},
                    "territory": {"data": {"type": "territories", "id": country_code}},
                },
            })

    if not price_refs:
        print("❌ No territories resolved to a price point - not creating the offer")
        return failures

    offer_name = config["offer_name"]
    offer_code = config["offer_code"]

    # Apple never frees up a name/offerCode once used, even after the offer that used it
    # is deleted (undocumented). If we just deleted an offer that had this exact
    # name/offerCode, recreating with the same values is guaranteed to collide - so on
    # that specific 409, retry with a uniquified name/offerCode instead of giving up.
    for attempt in range(5):
        body = {
            "data": {
                "type": "subscriptionPromotionalOffers",
                "attributes": {
                    "name": offer_name,
                    "offerCode": offer_code,
                    "duration": config["apple_duration"],
                    "offerMode": "PAY_AS_YOU_GO",
                    "numberOfPeriods": config["num_periods"],
                },
                "relationships": {
                    "subscription": {"data": {"type": "subscriptions", "id": subscription_id}},
                    "prices": {"data": price_refs},
                },
            },
            "included": included,
        }
        resp = requests.post(f"{base_url}/subscriptionPromotionalOffers", json=body, headers=headers, timeout=30)
        if resp.status_code == 201:
            print(f"✅ Created promotional offer '{offer_name}' (offer code '{offer_code}') covering {len(price_refs)} territories")
            break
        if resp.status_code == 409 and "ENTITY_ERROR.ATTRIBUTE.INVALID.DUPLICATE" in resp.text:
            suffix = f"-{int(time.time())}"
            offer_name = f"{config['offer_name']}{suffix}"
            offer_code = f"{config['offer_code']}{suffix}"
            print(f"   Name/offer code already used (even though deleted) - retrying as '{offer_code}' (attempt {attempt + 1})")
            time.sleep(1)
            continue
        reason = f"API Error {resp.status_code}: {resp.text}"
        print(f"❌ Failed to create offer: {reason}")
        failures.append(_make_apple_failure("N/A", "N/A", None, None, reason))
        break
    else:
        reason = f"Could not find a free name/offer code after {attempt + 1} attempts"
        print(f"❌ {reason}")
        failures.append(_make_apple_failure("N/A", "N/A", None, None, reason))

    return failures


def main():
    data = load_input_data()

    print("🚀 Starting Promotional Offer Applier...")
    print(f"📊 Loaded {len(data.country_prices)} countries from price_scaled.xlsx")

    if not _validate_env():
        return

    config = prompt_offer_config()

    print(f"\n💰 Discount: {config['discount_percent']}% for {config['num_periods']} cycle(s) of {config['apple_duration']}")

    failures = []
    if "google" in config["platforms"]:
        try:
            failures += create_google_offer(data, config)
        except Exception as e:
            print(f"❌ Google Play offer creation crashed: {e}")
            failures.append({
                "platform": "Google Play", "country": "N/A", "country_code": "N/A",
                "price": None, "currency": None, "reason": f"Unhandled exception: {e}",
            })
    if "apple" in config["platforms"]:
        try:
            failures += create_apple_offer(data, config)
        except Exception as e:
            print(f"❌ Apple offer creation crashed: {e}")
            failures.append(_make_apple_failure("N/A", "N/A", None, None, f"Unhandled exception: {e}"))

    print("\n📋 Creating failure report...")
    create_failure_report(failures)

    print("✅ Promotional offer process completed!")


if __name__ == "__main__":
    main()
