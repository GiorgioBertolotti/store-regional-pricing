# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo does

Three-step pipeline for scaling subscription prices by country (purchasing-power adjusted) and pushing them to App Store Connect and Google Play Console:

1. `cost-of-living.py` — fetches World Bank PPP conversion factors and restcountries.com currency codes → writes `cost_of_living_data.xlsx`
2. `price_scaler.py` — reads that Excel, fetches live exchange rates from `api.exchangerate-api.com`, computes per-country scaling factors based on a mid-range restaurant meal as the PPP anchor, applies country-specific VAT/GST, runs smart-pricing rounding, and writes `price_scaled.xlsx`
3. `subscription_price_applier.py` — reads `price_scaled.xlsx` + `country_codes.json`, then pushes prices to both stores via their APIs; failures are written to a timestamped `price_update_failures_*.txt` file
4. `promotional_offer_applier.py` (optional) — creates a discounted, time-limited promotional offer on top of the current live price: a `relativeDiscount` phase on Google Play, and a `subscriptionPromotionalOffers` resource on App Store Connect (built from a discounted `Smart_Price_Native` resolved to the closest price point). Imports its Apple/Google helpers directly from `subscription_price_applier.py` rather than duplicating them.

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

**Apple startDate**: price updates need a future `startDate`; "tomorrow" (UTC) is sometimes rejected as still too soon (undocumented minimum lead time, observed requiring at least 2 days) — `_submit_apple_price()` reads Apple's own "must be on or after YYYY-MM-DD" out of the 409 body and retries with that date instead of guessing a fixed offset.

**Apple concurrency**: price-point resolution is one HTTP round-trip per country (pure I/O wait), so `update_app_store_prices()` (`subscription_price_applier.py`) and the resolution loop in `create_apple_offer()` (`promotional_offer_applier.py`) both run it through a `ThreadPoolExecutor(max_workers=APPLE_MAX_WORKERS)` (defined once in `subscription_price_applier.py`, imported by the other) instead of a sequential loop — cuts a ~200-country run from minutes to well under a minute. `APPLE_MAX_WORKERS=8` is a deliberately conservative default against Apple's undocumented rate limits; console output from concurrent countries will interleave since prints happen inside worker threads.

**Google Play**: prices are written by patching the full subscription object in-place (not a per-region endpoint), which means every patch resubmits _all_ base plans, not just the one you're targeting — a pre-existing data problem on a base plan you have no intention of touching (e.g. a stale price left over from a currency changeover) can still block the whole patch. Currency must match the existing regional config or the update is skipped and logged as a failure. The patch loop auto-retries on recoverable 400 errors (stale currency codes, non-billable regions, price out of range via clamping to Google's minimum). If a clamp is applied to the same (base plan, region) pair twice without Google's validation ever converging, the region is dropped from just that one base plan (not the one being updated) so the rest of the patch can still go through. If Google then rejects *that* too (`"Regional configs were removed from the base plan"` — confirmed to happen for an existing/active region, unlike a not-yet-configured one), the script gives up immediately with an actionable `RuntimeError` pointing at the specific base plan/region to fix manually in Play Console, rather than retrying blindly — this specific combination is a Google-side data lock the API has no path to resolve.

**Country code mapping**: `country_codes.json` maps World Bank country names → ISO alpha-2 (Google) and alpha-3 (Apple). `update_country_codes.py` regenerates this file from the World Bank and restcountries.com APIs.

**Promotional offers** (`promotional_offer_applier.py`): prompts first for which platform(s) to target (Apple/Google/both). Google Play offers use `relativeDiscount` (a percentage off whatever price is currently live per region), so no price computation is needed there; regions Google reports as "not billable" are auto-dropped and retried (same pattern as the base price applier's 400-error recovery, reusing its `_NOT_BILLABLE_RE`). Apple has no percentage-discount concept — the script computes a discounted price from `Smart_Price_Native`, resolves it to a price point via `_resolve_apple_price_point()` (reused from `subscription_price_applier.py`, so the USD-retry fallback for territories Apple prices in USD applies here too), then sends it as a JSON:API compound document: the `prices` relationship references client-chosen ids wrapped as `${local-id}` (e.g. `${NOR}`), and a top-level `included` array supplies each one's `subscriptionPricePoint` + `territory` relationships. This endpoint is thinly documented by Apple and its runtime validation (the `${...}` id wrapping, the required `included` block) isn't reflected in Apple's own published OpenAPI schema — the shape was reverse-engineered from the live 409 error responses, not official docs. Apple's PATCH endpoint for this resource rejects the same payload shape its POST endpoint accepts (also undocumented) — since only POST is proven to work, re-running with the same `offerCode` deletes the existing offer and fully recreates it via POST rather than extending it in place, at the cost of a brief window where the offer doesn't exist. Apple additionally never frees up a `name`/`offerCode` once used, even after the offer is deleted (also undocumented) — a duplicate-attribute 409 on that recreate POST is auto-retried with a `-<unix timestamp>` suffix appended to both, rather than failing outright.
