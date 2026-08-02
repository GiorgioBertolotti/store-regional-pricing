"""Cost-of-living data + country-code mapping.

Merges the two ex-scripts that fetched reference data before any pricing math could run
(cost-of-living.py and update_country_codes.py). Both duplicated `_fetch_worldbank_paginated`
verbatim and had near-duplicate "fetch World Bank country names" functions; both are
collapsed to one copy each here.

Data sources:
  - PPP conversion factors: World Bank API (PA.NUS.PRVT.PP indicator)
  - Currency codes: pycountry (ISO 3166 country list) + Babel (CLDR
    territory-to-currency mapping) - both offline, no API key or network call
  - ISO alpha-2/alpha-3 country codes: World Bank (names) + pycountry (alpha-3), offline

Synthetic meal price:
  The scaler's PPP anchor is "Meal for 2 People, Mid-range Restaurant, Three-course",
  derived as meal_native = US_REFERENCE_MEAL_USD * ppp_factor. Since US PPP = 1.0, the US
  meal equals the reference price exactly and every other country scales off it. Note this
  constant is cosmetic: the scaling factor algebraically reduces to
  min(ppp_factor / fx_rate, 1.0) regardless of its value (see scaler.py). It's kept only so
  the spreadsheet has a human-readable reference column.
"""

from __future__ import annotations

import datetime
import json
import time
from pathlib import Path

import pandas as pd
import pycountry
import requests
from babel.numbers import get_territory_currencies

WORLD_BANK_BASE = "https://api.worldbank.org/v2"
PPP_INDICATOR = "PA.NUS.PRVT.PP"
MEAL_COLUMN = "Meal for 2 People, Mid-range Restaurant, Three-course"
US_REFERENCE_MEAL_USD = 80.0

COST_OF_LIVING_FILE = Path("cost_of_living_data.xlsx")
COUNTRY_CODES_FILE = Path("country_codes.json")

# Excluded: CLDR reports Palestine using ILS/JOD, but neither store supports
# it as a distinct billing territory.
_EXCLUDED_ISO2 = {"PS"}

# A cost-of-living cache is "fresh" if it was written for the current year's World Bank
# vintage (World Bank PPP data itself lags ~1-2 years, hence the fallback-year walk below).
FRESHNESS_DAYS = 30


def _get_with_retries(url: str, max_retries: int = 3) -> requests.Response:
    """GET with a short retry/backoff - World Bank's public API has no auth and is prone
    to occasional transient errors (observed: a bare 400 on an otherwise-valid query that
    succeeded immediately on retry) rather than anything this pipeline can fix."""
    last_error = None
    for attempt in range(max_retries):
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
    raise last_error


def _fetch_worldbank_paginated(url: str) -> list:
    """Fetch all pages from a World Bank list endpoint and return the concatenated items.

    World Bank paginates based on `per_page`; a single request only ever returns
    one page even if the query string asks for more items than exist per page,
    so results silently truncate without this if the true item count grows.
    """
    items = []
    page = 1
    while True:
        response = _get_with_retries(f"{url}&page={page}")
        data = response.json()
        if len(data) < 2 or not data[1]:
            break
        items.extend(data[1])
        if page >= data[0].get("pages", 1):
            break
        page += 1
    return items


def fetch_worldbank_country_names() -> dict[str, str]:
    """Return {iso2: world_bank_country_name} for all non-aggregate countries."""
    url = f"{WORLD_BANK_BASE}/country?format=json&per_page=300"
    items = _fetch_worldbank_paginated(url)
    if not items:
        raise ValueError("Unexpected World Bank country endpoint response")

    return {
        c["iso2Code"]: c["name"]
        for c in items
        if c.get("iso2Code") and c.get("region", {}).get("id") != "NA"
    }


def fetch_ppp_data(year: int) -> dict[str, float]:
    """Fetch PPP conversion factors from World Bank for a given year.

    Returns {iso2_code: ppp_factor} for countries with valid data. Regional
    aggregates (non-2-char codes) are excluded.
    """
    url = (
        f"{WORLD_BANK_BASE}/country/all/indicator/{PPP_INDICATOR}"
        f"?format=json&per_page=300&date={year}"
    )
    items = _fetch_worldbank_paginated(url)
    # An empty result here is routine, not an error: World Bank PPP data lags 1-2 years
    # behind the current year, so querying this year (or last year) can legitimately
    # return nothing yet. fetch_ppp_data_with_fallback() walks back through earlier years
    # to compensate - raising here would break that walk-back on every fresh checkout.
    if not items:
        return {}

    return {
        item["country"]["id"]: item["value"]
        for item in items
        if item["value"] is not None and len(item["country"]["id"]) == 2
    }


def fetch_ppp_data_with_fallback(ppp_year: str = "auto", max_lookback: int = 5) -> dict[str, tuple[float, int]]:
    """Fetch PPP data, walking back year by year to fill gaps.

    ppp_year="auto" starts from the current year; a specific year (e.g. "2024") pins the
    primary year but still falls back to earlier years for countries missing that year.
    Returns {iso2: (ppp_factor, year_used)}.
    """
    start_year = datetime.date.today().year if ppp_year == "auto" else int(ppp_year)

    merged: dict[str, tuple[float, int]] = {}
    for offset in range(max_lookback):
        year = start_year - offset
        year_data = fetch_ppp_data(year)
        filled = 0
        for iso2, val in year_data.items():
            if iso2 not in merged:
                merged[iso2] = (val, year)
                filled += 1
        if not year_data:
            print(f"  no data yet for {year}, trying an earlier year...")
        elif offset == 0:
            print(f"  {len(year_data)} countries found for {year}")
        elif filled:
            print(f"  {filled} additional countries filled from {year}")

    if not merged:
        raise RuntimeError(
            f"No World Bank PPP data found in the last {max_lookback} years starting from {start_year}"
        )
    return merged


def fetch_currency_codes() -> dict[str, str]:
    """Resolve ISO 4217 currency codes offline via pycountry + Babel's CLDR data.

    Returns {iso2_country_code: iso4217_currency_code}.

    A handful of territories legally accept a neighbor's currency alongside their own
    (Bhutan/India, Lesotho and Namibia/South Africa), so CLDR's tender-currency list for
    them has two entries - and Babel doesn't indicate which is primary. Taking entry [0]
    picked the neighbor's currency (e.g. INR for Bhutan, whose own currency is BTN) purely
    because of CLDR's list order. ISO 4217 codes are conventionally the country's own
    alpha-2 prefixed (BT -> BTN, NA -> NAD, LS -> LSL), so preferring a currency that
    starts with the territory's own alpha-2 code picks the territory's own currency
    whenever one of the candidates matches; otherwise it falls back to CLDR's order
    unchanged (e.g. Haiti's HTG already sorts first over USD).
    """
    today = datetime.date.today()
    result = {}
    for country in pycountry.countries:
        currencies = get_territory_currencies(country.alpha_2, today, today, tender=True, non_tender=False)
        if not currencies:
            continue
        result[country.alpha_2] = next(
            (c for c in currencies if c.startswith(country.alpha_2)), currencies[0]
        )
    return result


def build_iso2_to_iso3() -> dict[str, str]:
    """Return {iso2: iso3} from the offline pycountry ISO-3166 dataset."""
    return {c.alpha_2: c.alpha_3 for c in pycountry.countries}


def build_cost_of_living_dataframe(
    ppp_data: dict[str, tuple[float, int]],
    currency_codes: dict[str, str],
    country_names: dict[str, str],
) -> pd.DataFrame:
    """Merge PPP and currency data into the DataFrame the scaler expects."""
    rows, skipped = [], []

    for iso2, (ppp_factor, year) in sorted(ppp_data.items(), key=lambda x: country_names.get(x[0], x[0])):
        if iso2 in _EXCLUDED_ISO2:
            continue

        currency = currency_codes.get(iso2)
        name = country_names.get(iso2)

        if not currency:
            skipped.append(f"{iso2} (no currency code)")
            continue
        if not name:
            skipped.append(f"{iso2} (no country name)")
            continue

        rows.append({
            "CountryName": name,
            "CurrencyCode": currency,
            MEAL_COLUMN: US_REFERENCE_MEAL_USD * ppp_factor,
            "PPP_Factor": ppp_factor,
            "PPP_Year": year,
        })

    if skipped:
        print(f"Skipped {len(skipped)} entries: {skipped}")

    return pd.DataFrame(rows)


def is_cost_of_living_stale(path: Path = COST_OF_LIVING_FILE) -> bool:
    """True if the cache is missing or older than FRESHNESS_DAYS."""
    if not path.is_file():
        return True
    age_days = (datetime.date.today() - datetime.date.fromtimestamp(path.stat().st_mtime)).days
    return age_days > FRESHNESS_DAYS


def refresh_cost_of_living(ppp_year: str = "auto", path: Path = COST_OF_LIVING_FILE) -> pd.DataFrame:
    """Fetch PPP + currency data and write cost_of_living_data.xlsx. Returns the DataFrame."""
    print("Fetching World Bank PPP data...")
    ppp_data = fetch_ppp_data_with_fallback(ppp_year)

    print("Resolving currency codes via pycountry/Babel...")
    currency_codes = fetch_currency_codes()

    print("Fetching country names from World Bank...")
    country_names = fetch_worldbank_country_names()
    print(f"  {len(country_names)} country names loaded")

    df = build_cost_of_living_dataframe(ppp_data, currency_codes, country_names)
    print(f"{len(df)} countries ready")

    df.to_excel(path, index=False)
    print(f"Saved to {path}")
    return df


def load_existing_country_codes(path: Path = COUNTRY_CODES_FILE) -> dict:
    try:
        with path.open() as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def refresh_country_codes(
    cost_of_living_path: Path = COST_OF_LIVING_FILE,
    country_codes_path: Path = COUNTRY_CODES_FILE,
) -> dict:
    """Regenerate country_codes.json from cost_of_living_data.xlsx's CountryName column.

    Merge-only: existing entries are kept and only added to/updated, never removed, since
    a stale entry for a country no longer in the World Bank dataset is still harmless to
    keep around and may be a country temporarily missing this year's PPP data.
    """
    df = pd.read_excel(cost_of_living_path)
    if "CountryName" not in df.columns:
        raise ValueError(f"CountryName column missing in {cost_of_living_path}")

    country_names_in_file: list[str] = df["CountryName"].dropna().unique().tolist()

    print("Fetching country names from World Bank...")
    wb_iso2_to_name = fetch_worldbank_country_names()
    wb_name_to_iso2 = {v: k for k, v in wb_iso2_to_name.items()}

    iso2_to_iso3 = build_iso2_to_iso3()

    existing = load_existing_country_codes(country_codes_path)
    updated = dict(existing)
    added, skipped = [], []

    for name in sorted(country_names_in_file):
        iso2 = wb_name_to_iso2.get(name)
        if not iso2:
            skipped.append(f"{name} (no ISO2 from World Bank)")
            continue
        iso3 = iso2_to_iso3.get(iso2)
        if not iso3:
            skipped.append(f"{name} ({iso2}, no ISO3 from pycountry)")
            continue

        entry = {"alpha2": iso2, "alpha3": iso3}
        if updated.get(name) != entry:
            updated[name] = entry
            added.append(name)

    if skipped:
        print(f"Skipped {len(skipped)} countries: {skipped}")

    with country_codes_path.open("w") as f:
        json.dump(updated, f, indent=2, ensure_ascii=False, sort_keys=True)

    print(f"country_codes.json updated: {len(updated)} total entries"
          + (f", {len(added)} new/changed" if added else " (no changes needed)"))
    return updated


def refresh_all(ppp_year: str = "auto") -> None:
    """Refresh both cost_of_living_data.xlsx and country_codes.json, in the order that
    keeps them consistent (country codes are derived from the cost-of-living country list)."""
    refresh_cost_of_living(ppp_year)
    refresh_country_codes()
