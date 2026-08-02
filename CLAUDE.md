# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo does

`store-regional-pricing` is a single guided CLI (`pricing.py`) for scaling subscription prices by country (purchasing power parity / PPP adjusted) and pushing them to App Store Connect and Google Play Console. Logic lives in the `store_pricing/` package; `pricing.py` is a thin entry point.

```
store_pricing/
  config.py    Settings dataclass: .env + pricing.toml -> passed as a parameter (never module globals)
  data.py      cost-of-living + country-code fetching (World Bank PPP + pycountry/Babel), writes cost_of_living_data.xlsx and country_codes.json
  scaler.py    price scaling math: exchange rates, PPP scaling factor, VAT, psychological rounding -> price_scaled.xlsx
  inputs.py    InputData: loads price_scaled.xlsx + country_codes.json for the apply/offer steps
  google.py    Google Play price updates via the Android Publisher API (the 400-error recovery ladder lives here)
  apple.py     App Store Connect price updates via the App Store Connect API (price-point resolution, JWT, startDate retry)
  offers.py    Promotional/discount offers on both platforms, built on top of apple.py/google.py
  wizard.py    `setup` (guided credential entry with live API verification) and `doctor` (re-verify without touching .env)
  report.py    Shared failure-report writer
  ui.py        Shared rich/questionary rendering: price preview, live-price diff, confirmations
  cli.py       argparse subcommands + the guided no-args flow
```

`pricing.py` with no arguments runs the guided flow: refresh cached data if stale → run the credential wizard if nothing's configured → prompt for a USD price → preview the computed table → optionally push, showing a diff against what's currently live and requiring confirmation before anything is written. Subcommands (`setup`, `doctor`, `scale`, `apply`, `offer`, `refresh-data`) cover the same ground non-interactively for CI, each falling back to a prompt for any flag left unset.

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python pricing.py setup   # or just `python pricing.py` and let the guided flow trigger it
```

## Running the pipeline

```bash
python pricing.py                                          # guided flow
python pricing.py doctor                                    # live-verify configured credentials
python pricing.py scale --price 19.99                       # compute only, no store writes
python pricing.py apply --dry-run                            # resolve everything, push nothing
python pricing.py apply --yes                                 # push live, no confirmation (CI)
python pricing.py offer --discount 30 --cycles 3 --period P1M --code SUMMER30 --google-offer-id summer-30-off
python pricing.py refresh-data                               # regenerate cost_of_living_data.xlsx + country_codes.json
```

## Required credentials (`.env`)

Same variable names as before; `pricing.py setup` writes this file for you and live-verifies each value against the real API as it's entered, rather than leaving verification to whenever `apply` first hits the API.

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

`store_pricing/config.py` parses these into `GoogleCreds`/`AppleCreds` and reports **all** missing/malformed vars at once (not first-failure-only like the old `_validate_env()`), so `Settings.apple_configured`/`.google_configured` let an Apple-only or Google-only setup run without the other platform's vars being present at all.

`.env` wins for every variable it defines; anything it omits falls through to the **process environment**, so CI can inject these as secrets instead of writing a `.env` file (matching what the pre-package `load_dotenv()` + `os.getenv()` did). Values still set to `.env.example`'s `your_*` placeholders are rejected as unconfigured rather than being passed to the API.

## `pricing.toml`

Generated with commented defaults on first run (see `pricing.toml.example` for the reference copy, or `store_pricing/config.py`'s `TOML_TEMPLATE`). Covers what used to be hardcoded constants: `anchor_country` (`price_scaler.py`'s old `reference_country`), `scaling_cap`, `default_vat` + per-country `[pricing.vat]` overrides, `rounding`, Apple's price-point tolerance (`price_point_tolerance_pct`/`_units`), `apple.max_workers`, and `data.ppp_year`.

Values are validated at load time and raise `ConfigError` naming the offending key, rather than surfacing later as a mid-run crash or a silently wrong price — an unrecognised `rounding` used to fall through to psychological rounding, so a typo looked like it worked, and `max_workers = 0` blew up inside `ThreadPoolExecutor` only once a run was already underway.

## Key design decisions

**Pricing algorithm** (`store_pricing/scaler.py`): scaling factor is capped at `pricing.scaling_cap` (default 1.0) — countries more expensive than the anchor country get the full USD price; only cheaper countries get a discount. Formula: `scaled_native = usd_price × scaling_factor × exchange_rate × (1 + vat_rate)`, then smart-pricing rounds to `.99` endings. `apply_smart_pricing()` and `PricingConfig.vat_rate()` are pure functions (no I/O, no side effects) specifically so they're unit-testable — see `tests/test_scaler.py`. The `US_REFERENCE_MEAL_USD` constant in `data.py` (the "meal for 2" PPP anchor) is cosmetic: the scaling factor algebraically reduces to `min(ppp_factor / fx_rate, scaling_cap)` regardless of its value.

**Apple price matching**: the App Store only allows prices from a fixed set of price points. `get_closest_price_point()` (`store_pricing/apple.py`) finds the nearest available point; it skips if the difference exceeds `pricing.toml`'s `apple.price_point_tolerance_pct`/`_units` (default 10% or 2 units of local currency). This is why some countries appear in failure reports.

**Apple startDate**: price updates need a future `startDate`; "tomorrow" (UTC) is sometimes rejected as still too soon (undocumented minimum lead time, observed requiring at least 2 days) — `_submit_price()` reads Apple's own "must be on or after YYYY-MM-DD" out of the 409 body and retries with that date instead of guessing a fixed offset.

**Apple concurrency**: price-point resolution and live-price fetching are each one HTTP round-trip per country (pure I/O wait), so `apple.update_prices()`, `apple.fetch_live_prices()`, and the resolution loop in `offers.create_apple_offer()` all run through a `ThreadPoolExecutor(max_workers=pricing.apple_max_workers)` instead of a sequential loop — cuts a ~200-country run from minutes (or, for the live-price fetch, a near-hang) to well under a minute. The default of 8 is a deliberately conservative default against Apple's undocumented rate limits; console output from concurrent countries will interleave since prints happen inside worker threads.

**Google Play**: prices are written by patching the full subscription object in-place (not a per-region endpoint), which means every patch resubmits _all_ base plans, not just the one you're targeting — a pre-existing data problem on a base plan you have no intention of touching (e.g. a stale price left over from a currency changeover) can still block the whole patch. Google rejects any price whose currency doesn't match what's already configured for that region (there's no in-place currency change via this endpoint), usually a leftover from an earlier flat-price rollout that never localized. Rather than skip and log a failure the way the pre-package script did, `_update_region()` converts the computed price into whatever currency Google already has on file (`scaler.convert_to_currency()`, shared with Apple's differing-territory-currency retry) and writes that — so those regions now get updated instead of failing every run. Only a region with no available exchange rate still fails; the apply-time diff predicts which is which, showing `fx convert` vs `locked`. The patch loop (`store_pricing/google.py`) auto-retries on recoverable 400 errors (stale currency codes, non-billable regions, price out of range via clamping to Google's minimum). If a clamp is applied to the same (base plan, region) pair twice without Google's validation ever converging, the region is dropped from just that one base plan (not the one being updated) so the rest of the patch can still go through. If Google then rejects _that_ too (`"Regional configs were removed from the base plan"` — confirmed to happen for an existing/active region, unlike a not-yet-configured one), the script gives up immediately with an actionable `RuntimeError` pointing at the specific base plan/region to fix manually in Play Console, rather than retrying blindly — this specific combination is a Google-side data lock the API has no path to resolve.

**Google live-price keying**: Google's regional configs are keyed by ISO alpha-2 region code, but everything else in the pipeline (`InputData.country_prices`, the diff table) is keyed by country name — `google.fetch_live_prices()` reverses `country_code_mapping` to translate as it reads, so callers never see the mismatch.

**Country code mapping**: `country_codes.json` maps World Bank country names → ISO alpha-2 (Google) and alpha-3 (Apple). `store_pricing/data.py`'s `refresh_country_codes()` (formerly `update_country_codes.py`) regenerates this file from the World Bank API (alpha-2 + names) and `pycountry` (alpha-3, offline), merge-only — existing entries are never dropped. restcountries.com's free v3.1 API was retired (301s to a deprecated-endpoint error body); its v5 replacement requires a signed-up API key, so both this and `data.py`'s currency-code lookup were moved off it entirely onto offline datasets (`pycountry` + Babel's CLDR territory data) rather than adding a new required credential.

**Promotional offers** (`store_pricing/offers.py`): `pricing.py offer` prompts first for which platform(s) to target (Apple/Google/both, or whichever is configured if only one is), then for the fields it needs. Google Play offers use `relativeDiscount` (a percentage off whatever price is currently live per region), so no price computation is needed there; regions Google reports as "not billable" or with the discounted price "out of range" are auto-dropped and retried (same pattern as the base price applier's 400-error recovery, reusing `google._NOT_BILLABLE_RE`). Apple has no percentage-discount concept — the code computes a discounted price from `Smart_Price_Native`, resolves it to a price point via `apple.resolve_price_point()` (reused from the base price path, so the USD-retry fallback for territories Apple prices in USD applies here too), then sends it as a JSON:API compound document: the `prices` relationship references client-chosen ids wrapped as `${local-id}` (e.g. `${NOR}`), and a top-level `included` array supplies each one's `subscriptionPricePoint` + `territory` relationships. This endpoint is thinly documented by Apple and its runtime validation (the `${...}` id wrapping, the required `included` block) isn't reflected in Apple's own published OpenAPI schema — the shape was reverse-engineered from the live 409 error responses, not official docs; `tests/test_apple_payload.py` locks the shape down. Apple's PATCH endpoint for this resource rejects the same payload shape its POST endpoint accepts (also undocumented) — since only POST is proven to work, re-running with the same `offerCode` deletes the existing offer and fully recreates it via POST rather than extending it in place, at the cost of a brief window where the offer doesn't exist; `offers.find_existing_apple_offer()` is exposed separately so the CLI can warn and confirm _before_ committing to that. Apple additionally never frees up a `name`/`offerCode` once used, even after the offer is deleted (also undocumented) — a duplicate-attribute 409 on that recreate POST is auto-retried with a `-<unix timestamp>` suffix appended to both, rather than failing outright.

## Tests

`tests/` covers the pricing math (`test_scaler.py`), Google's 400-error recovery ladder (`test_google_errors.py`), the Apple promotional-offer payload shape (`test_apple_payload.py`), and PPP-year fallback + country-code merge semantics (`test_data.py`). Run with `pytest` after `pip install -r requirements-dev.txt`. These are pure-logic tests with network calls monkeypatched out — they don't touch the real APIs.
