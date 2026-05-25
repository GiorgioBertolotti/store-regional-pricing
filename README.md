# store-regional-pricing

Automate purchasing-power-adjusted subscription pricing across 150+ countries and apply it directly to **App Store Connect** and **Google Play Console** in minutes — no spreadsheet juggling, no manual price entry per territory.

## Why this exists

Flat USD prices are a poor fit for a global audience. A $19.99/year subscription is affordable in the US but prohibitively expensive in India, Brazil, or Southeast Asia — and overpriced in Switzerland or Norway is often still cheap compared to local alternatives.

Most tools stop at producing a spreadsheet. This pipeline goes further:

- **Anchors prices to real purchasing power** using World Bank PPP conversion factors (not arbitrary discounts)
- **Applies live exchange rates** so prices stay accurate as currencies shift
- **Accounts for local VAT/GST** — the price the user sees already includes tax
- **Rounds to psychological price points** (`.99` endings, platform-native tiers)
- **Pushes directly to both stores** via their official APIs — App Store Connect and Google Play Console — in a single command

The result is a regionally fair price ladder that maximises conversions across markets without manual effort on every price change.

---

## How it works

The pipeline runs in three steps:

```
cost-of-living.py  →  price_scaler.py  →  subscription_price_applier.py
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

---

## Setup

```bash
git clone https://github.com/your-username/store-regional-pricing.git
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

---

## Country code mapping

`country_codes.json` maps World Bank country names to ISO alpha-2 (Google Play) and alpha-3 (App Store Connect) codes. The file is included in the repo and covers all countries available in the World Bank PPP dataset.

If new countries appear after a data refresh, regenerate it:

```bash
python update_country_codes.py
```

---

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

---

## File overview

| File                            | Purpose                                                         |
| ------------------------------- | --------------------------------------------------------------- |
| `cost-of-living.py`             | Fetch PPP + currency data → `cost_of_living_data.xlsx`          |
| `price_scaler.py`               | Scale USD price per country → `price_scaled.xlsx`               |
| `subscription_price_applier.py` | Push prices to both stores                                      |
| `update_country_codes.py`       | Regenerate `country_codes.json` from World Bank + restcountries |
| `country_codes.json`            | Country name → ISO alpha-2/alpha-3 mapping                      |
| `.env.example`                  | Template for credentials                                        |
| `requirements.txt`              | Python dependencies                                             |

---

## Requirements

- Python 3.9+
- A Google Play Console service account with Manage store presence permission
- An App Store Connect API key with Finance role
- A free [exchangerate-api.com](https://www.exchangerate-api.com) account (1 500 req/month on the free tier — more than enough for monthly runs)

---

## License

MIT
