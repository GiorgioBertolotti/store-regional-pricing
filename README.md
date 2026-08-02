# store-regional-pricing

[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![App Store Connect API](https://img.shields.io/badge/App%20Store%20Connect-API-000000?logo=apple&logoColor=white)](https://developer.apple.com/documentation/appstoreconnectapi)
[![Google Play Developer API](https://img.shields.io/badge/Google%20Play-Developer%20API-3DDC84?logo=googleplay&logoColor=white)](https://developers.google.com/android-publisher)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](#license)

**Purchasing-power-parity (PPP) subscription pricing automation** for App Store Connect and Google Play Console. Scale a single USD price into 150+ localized, tax-inclusive, psychologically-rounded prices — anchored to real cost-of-living data, not flat exchange rates — and push them live to both stores via their official APIs. No spreadsheet juggling, no manual price entry per territory.

## Table of contents

- [Table of contents](#table-of-contents)
- [Why this exists](#why-this-exists)
- [Features](#features)
- [How it works](#how-it-works)
  - [Step 1 — Fetch cost-of-living data](#step-1--fetch-cost-of-living-data)
  - [Step 2 — Scale prices](#step-2--scale-prices)
  - [Step 3 — Apply to both stores](#step-3--apply-to-both-stores)
  - [Step 4 (optional) — Create a promotional offer](#step-4-optional--create-a-promotional-offer)
- [Setup](#setup)
  - [Credentials](#credentials)
    - [Where to find each value](#where-to-find-each-value)
- [Country code mapping](#country-code-mapping)
- [Pricing algorithm](#pricing-algorithm)
- [FAQ](#faq)
- [How this compares to store-native regional pricing](#how-this-compares-to-store-native-regional-pricing)
- [Requirements](#requirements)
- [License](#license)

## Why this exists

Flat USD prices are a poor fit for a global audience. A $19.99/year subscription is affordable in the US but prohibitively expensive in India, Brazil, or Southeast Asia — and a straight exchange-rate conversion still overprices it relative to local income almost everywhere outside North America and Western Europe.

Most regional-pricing tools stop at producing a spreadsheet you then re-key by hand into App Store Connect and Google Play Console. This pipeline goes further and pushes the result live:

- **Anchors prices to real purchasing power** using World Bank PPP conversion factors (not arbitrary regional discounts)
- **Applies live exchange rates** so prices stay accurate as currencies shift
- **Accounts for local VAT/GST** — the price the user sees already includes tax
- **Rounds to psychological price points** (`.99` endings, platform-native tiers)
- **Pushes directly to both stores** via their official APIs — App Store Connect and Google Play Console — in a single command
- **Automates promotional/discount offers** on both platforms from the same pricing data

The result is a regionally fair price ladder that maximizes conversions across markets without manual effort on every price change.

## Features

| Capability                                | App Store Connect          | Google Play Console        |
| ----------------------------------------- | -------------------------- | -------------------------- |
| PPP-adjusted base pricing                 | ✅                         | ✅                         |
| Live exchange-rate updates                | ✅                         | ✅                         |
| VAT/GST-inclusive pricing                 | ✅                         | ✅                         |
| Psychological price rounding (`.99`)      | ✅                         | ✅                         |
| Direct API price push                     | ✅ (App Store Connect API) | ✅ (Android Publisher API) |
| Regional promotional/discount offers      | ✅                         | ✅                         |
| Automatic retry on recoverable API errors | ✅                         | ✅                         |
| Failure report per run                    | ✅                         | ✅                         |

## How it works

The pipeline runs as a sequence of standalone Python scripts:

```
cost-of-living.py  →  price_scaler.py  →  subscription_price_applier.py  →  promotional_offer_applier.py
```

### Step 1 — Fetch cost-of-living data

```bash
python cost-of-living.py
```

Pulls **World Bank PPP conversion factors** (indicator `PA.NUS.PRVT.PP`) and **currency codes** from restcountries.com. Derives a synthetic "meal for 2 at a mid-range restaurant" price per country as the PPP anchor, then writes `cost_of_living_data.xlsx`.

No scraping. No browser automation. Two public REST APIs, ~10 seconds.

### Step 2 — Scale prices

```bash
python price_scaler.py
```

Prompts for your USD base price. For each country it:

1. Fetches live exchange rates from [exchangerate-api.com](https://www.exchangerate-api.com) (free tier)
2. Computes a scaling factor capped at 1.0 (expensive countries pay the full USD price; cheaper countries get a proportional discount)
3. Applies the country's VAT/GST rate
4. Rounds to the nearest psychological price point

Writes `price_scaled.xlsx` with one row per country.

### Step 3 — Apply to both stores

```bash
python subscription_price_applier.py
```

Reads `price_scaled.xlsx` and `country_codes.json`, then:

- **Google Play**: fetches the subscription object, patches all regional configs in a single API call, auto-retries on recoverable 400 errors (stale currency, non-billable region, price out of range)
- **App Store Connect**: generates a JWT, fetches available price points per territory, matches the closest one within a 10%/2-unit tolerance, and submits the price update

Any country that could not be updated is logged to a timestamped `price_update_failures_YYYYMMDD_HHMMSS.txt` report.

### Step 4 (optional) — Create a promotional offer

```bash
python promotional_offer_applier.py
```

Prompts for which platform(s) to target (Apple/Google/both), a discount percentage, number of billing cycles, billing period, and offer name/codes, then reuses `price_scaled.xlsx` and the store credentials to create a matching discounted offer:

- **Google Play**: creates a `basePlans.offers` resource with a `relativeDiscount` phase (percentage off whatever price is currently live in each region) and activates it
- **App Store Connect**: computes the discounted price per territory from `Smart_Price_Native`, resolves it to the closest available price point, and creates a `subscriptionPromotionalOffers` resource covering all resolved territories

Apple's promotional offer API is thinly documented; if it changes shape the Apple half fails with the raw API error while the Google half is unaffected. Failures are logged to the same `price_update_failures_*.txt` report format.

## Setup

```bash
git clone https://github.com/GiorgioBertolotti/store-regional-pricing.git
cd store-regional-pricing

python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -r requirements.txt
cp .env.example .env            # fill in your credentials
```

### Credentials

Edit `.env` with your store credentials:

```env
# Google Play Console
GOOGLE_SERVICE_ACCOUNT_FILE=service-account-file.json
GOOGLE_PACKAGE_NAME=com.example.app
GOOGLE_SUBSCRIPTION_ID=your_subscription_id
GOOGLE_BASEPLAN_ID=your_base_plan_id

# App Store Connect
APPLE_ISSUER_ID=your_issuer_id
APPLE_KEY_ID=your_api_key_id
APPLE_PRIVATE_KEY=-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----
APPLE_APP_ID=your_numeric_app_id
APPLE_SUBSCRIPTION_PRODUCT_ID=your_product_id
```

#### Where to find each value

**Google Play**

| Variable                      | Where to find it                                                                  |
| ----------------------------- | --------------------------------------------------------------------------------- |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Google Play Console → Setup → API access → Create service account → download JSON |
| `GOOGLE_PACKAGE_NAME`         | Your app's package name (e.g. `com.example.app`)                                  |
| `GOOGLE_SUBSCRIPTION_ID`      | Play Console → Monetize → Subscriptions → product ID                              |
| `GOOGLE_BASEPLAN_ID`          | Inside the subscription → Base plans → base plan ID                               |

The service account needs the **"Manage store presence"** permission in Play Console.

**App Store Connect**

| Variable                        | Where to find it                                                   |
| ------------------------------- | ------------------------------------------------------------------ |
| `APPLE_ISSUER_ID`               | App Store Connect → Users & Access → Keys → Issuer ID              |
| `APPLE_KEY_ID`                  | Same page → your API key's Key ID                                  |
| `APPLE_PRIVATE_KEY`             | Download the `.p8` file; paste its contents with `\n` for newlines |
| `APPLE_APP_ID`                  | App Store Connect → your app → General → Numeric App ID in the URL |
| `APPLE_SUBSCRIPTION_PRODUCT_ID` | App Store Connect → your app → Subscriptions → product ID          |

The API key needs the **"Finance"** role.

## Country code mapping

`country_codes.json` maps World Bank country names to ISO alpha-2 (Google Play) and alpha-3 (App Store Connect) codes. The file is included in the repo and covers all countries available in the World Bank PPP dataset.

If new countries appear after a data refresh, regenerate it:

```bash
python update_country_codes.py
```

## Pricing algorithm

```
scaling_factor = min(us_meal_usd / country_meal_usd, 1.0)
scaled_native  = usd_price × scaling_factor × exchange_rate × (1 + vat_rate)
final_price    = smart_round(scaled_native)   # → .99 endings
```

- Countries with a higher cost of living than the US receive the full USD price (factor capped at 1.0)
- VAT/GST rates are embedded for ~50 countries; unlisted countries default to 15%
- Smart rounding snaps prices to platform-native tiers (e.g. `$9.99`, `$4.99`)

**Apple price matching**: the App Store enforces a fixed set of price points per territory. The script finds the closest available point and skips the update if the difference exceeds 10% or 2 units of local currency. Skipped territories appear in the failure report.

**Google Play**: the entire subscription object is patched in one call. The script retries automatically on recoverable 400 errors: stale currency codes, non-billable regions, and prices outside the allowed range.

## FAQ

**What is purchasing power parity (PPP) pricing for apps?**
PPP pricing sets a subscription's local price based on what an equivalent basket of goods costs in that country, rather than a flat currency conversion. It's why a $9.99 subscription might localize to $3.99-equivalent in a lower-income market instead of a mechanical exchange-rate conversion of $9.99 — the local price reflects local purchasing power, not just the currency rate.

**How do I set different subscription prices per country on the App Store or Google Play?**
Both platforms let you set prices manually per territory in their respective consoles, but doing that for 150+ countries by hand — and keeping it in sync as exchange rates move — doesn't scale. This repo automates it end-to-end: `price_scaler.py` computes the per-country price, `subscription_price_applier.py` pushes it via the App Store Connect API and Android Publisher API.

**Does Google Play support PPP-based regional pricing automatically?**
Google Play's own "price templates" convert by exchange rate, not purchasing power, and Apple's App Store Connect has no built-in PPP concept at all. Neither platform anchors to cost-of-living data out of the box — this pipeline computes that anchor from World Bank data and applies it to both stores identically.

**Can I automate App Store Connect price updates via API instead of the dashboard?**
Yes — `subscription_price_applier.py` authenticates with a JWT signed by an App Store Connect API key, resolves each territory to the closest available price point, and submits the update programmatically. No manual per-territory entry in the dashboard.

**How do I create a regional promotional/discount offer on Google Play and App Store Connect?**
Run `python promotional_offer_applier.py` after the base prices are live. It creates a Google Play `relativeDiscount` offer and an App Store Connect `subscriptionPromotionalOffers` resource from the same underlying pricing data, so the discount is consistent across both stores.

**Why not just use App Store Connect's "equalize prices" or Google Play's currency conversion tool?**
See [How this compares to store-native regional pricing](#how-this-compares-to-store-native-regional-pricing) below.

## How this compares to store-native regional pricing

Apple and Google both offer semi-automatic regional price tools — App Store Connect's price equalization and Google Play's currency-based price templates. Both convert by **exchange rate only**: a $9.99 subscription becomes exchange-rate-equivalent in Vietnam and in Switzerland, even though average purchasing power between those markets differs by an order of magnitude. Neither platform factors in local VAT/GST inclusivity, cost-of-living, or psychological price rounding, and neither exposes a way to push a **PPP-adjusted** price ladder programmatically.

This pipeline anchors every price to World Bank PPP conversion factors instead of raw FX rates, so the local price reflects what people can actually afford to pay — then handles tax inclusivity, `.99` rounding, and the API push to both stores in one run.

## Requirements

- Python 3.9+
- A Google Play Console service account with Manage store presence permission
- An App Store Connect API key with Finance role
- A free [exchangerate-api.com](https://www.exchangerate-api.com) account (1 500 req/month on the free tier — more than enough for monthly runs)

## License

MIT
