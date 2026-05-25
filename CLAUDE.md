# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo does

Three-step pipeline for scaling subscription prices by country (purchasing-power adjusted) and pushing them to App Store Connect and Google Play Console:

1. `cost-of-living.py` — fetches World Bank PPP conversion factors and restcountries.com currency codes → writes `cost_of_living_data.xlsx`
2. `price_scaler.py` — reads that Excel, fetches live exchange rates from `api.exchangerate-api.com`, computes per-country scaling factors based on a mid-range restaurant meal as the PPP anchor, applies country-specific VAT/GST, runs smart-pricing rounding, and writes `price_scaled.xlsx`
3. `subscription_price_applier.py` — reads `price_scaled.xlsx` + `country_codes.json`, then pushes prices to both stores via their APIs; failures are written to a timestamped `price_update_failures_*.txt` file

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in credentials
```

## Running the pipeline

```bash
# Step 1 — fetch PPP data and currency codes (~10 s, no scraping)
python cost-of-living.py

# Step 2 — interactive: prompts for USD base price
python price_scaler.py

# Step 3 — pushes live to both stores
python subscription_price_applier.py

# Optional: regenerate country_codes.json after a dataset refresh
python update_country_codes.py
```

## Required credentials (`.env`)

| Variable                        | Source                                                       |
| ------------------------------- | ------------------------------------------------------------ |
| `GOOGLE_SERVICE_ACCOUNT_FILE`   | Path to `service-account-file.json` from Google Play Console |
| `GOOGLE_PACKAGE_NAME`           | App package name                                             |
| `GOOGLE_SUBSCRIPTION_ID`        | Subscription product ID in Play Console                      |
| `GOOGLE_BASEPLAN_ID`            | Base plan ID within that subscription                        |
| `APPLE_ISSUER_ID`               | App Store Connect → Keys → Issuer ID                         |
| `APPLE_KEY_ID`                  | API key ID                                                   |
| `APPLE_PRIVATE_KEY`             | Full private key contents (use `\n` for newlines in `.env`)  |
| `APPLE_APP_ID`                  | Numeric App ID (not bundle ID)                               |
| `APPLE_SUBSCRIPTION_PRODUCT_ID` | Product ID of the subscription in-app purchase               |

## Key design decisions

**Pricing algorithm** (`price_scaler.py`): scaling factor is capped at 1.0 — countries more expensive than the US get the full USD price; only cheaper countries get a discount. Formula: `scaled_native = usd_price × scaling_factor × exchange_rate × (1 + vat_rate)`, then smart-pricing rounds to `.99` endings.

**Apple price matching**: the App Store only allows prices from a fixed set of price points. `get_closest_price_point()` finds the nearest available point; it skips if the difference exceeds 10% or 2 units of the local currency. This is why some countries appear in failure reports.

**Google Play**: prices are written by patching the full subscription object in-place (not a per-region endpoint). Currency must match the existing regional config or the update is skipped and logged as a failure. The patch loop auto-retries on recoverable 400 errors (stale currency codes, non-billable regions, price out of range).

**Country code mapping**: `country_codes.json` maps World Bank country names → ISO alpha-2 (Google) and alpha-3 (Apple). `update_country_codes.py` regenerates this file from the World Bank and restcountries.com APIs.
