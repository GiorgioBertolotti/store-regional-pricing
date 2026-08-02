"""
Fetches cost-of-living data using World Bank PPP conversion factors, with
currency codes resolved offline from pycountry + Babel's CLDR territory data.

Data sources:
  - PPP conversion factors: World Bank API (PA.NUS.PRVT.PP indicator)
  - Currency codes: pycountry (ISO 3166 country list) + Babel (CLDR
    territory-to-currency mapping) — both offline, no API key or network call

Synthetic meal price:
  The price_scaler.py anchor is "Meal for 2 People, Mid-range Restaurant, Three-course".
  We derive it as: meal_native = US_REFERENCE_MEAL_USD * ppp_factor
  PPP factor is LCU per international dollar; since US PPP = 1.0, the US meal
  equals the reference price exactly, and all other countries scale accordingly.

Output: cost_of_living_data.xlsx (same format expected by price_scaler.py)
"""

from __future__ import annotations

import datetime

import pycountry
import requests
import pandas as pd
import colorama
from babel.numbers import get_territory_currencies
from colorama import Fore

colorama.init()

WORLD_BANK_BASE = "https://api.worldbank.org/v2"
PPP_INDICATOR = "PA.NUS.PRVT.PP"
MEAL_COLUMN = "Meal for 2 People, Mid-range Restaurant, Three-course"
US_REFERENCE_MEAL_USD = 80.0

# Excluded: CLDR reports Palestine using ILS/JOD, but neither store supports
# it as a distinct billing territory.
_EXCLUDED_ISO2 = {"PS"}


def _fetch_worldbank_paginated(url: str) -> list:
    """Fetch all pages from a World Bank list endpoint and return the concatenated items.

    World Bank paginates based on `per_page`; a single request only ever returns
    one page even if the query string asks for more items than exist per page,
    so results silently truncate without this if the true item count grows.
    """
    items = []
    page = 1
    while True:
        response = requests.get(f"{url}&page={page}", timeout=30)
        response.raise_for_status()
        data = response.json()
        if len(data) < 2 or not data[1]:
            break
        items.extend(data[1])
        if page >= data[0].get("pages", 1):
            break
        page += 1
    return items


def fetch_ppp_data(year: int) -> dict[str, float]:
    """Fetch PPP conversion factors from World Bank for a given year.

    Returns a dict of {iso2_code: ppp_factor} for countries with valid data.
    Regional aggregates (non-2-char codes) are excluded.
    """
    url = (
        f"{WORLD_BANK_BASE}/country/all/indicator/{PPP_INDICATOR}"
        f"?format=json&per_page=300&date={year}"
    )
    items = _fetch_worldbank_paginated(url)
    if not items:
        raise ValueError(f"Unexpected World Bank API response for year {year}")

    return {
        item["country"]["id"]: item["value"]
        for item in items
        if item["value"] is not None and len(item["country"]["id"]) == 2
    }


def fetch_ppp_data_with_fallback(primary_year: int, fallback_year: int) -> dict[str, tuple[float, int]]:
    """Fetch PPP data from primary year, falling back to fallback year for gaps.

    Returns {iso2: (ppp_factor, year_used)}.
    """
    print(Fore.CYAN + f"Fetching World Bank PPP data for {primary_year}..." + Fore.RESET)
    primary = fetch_ppp_data(primary_year)
    print(Fore.GREEN + f"  {len(primary)} countries found for {primary_year}" + Fore.RESET)

    print(Fore.CYAN + f"Fetching World Bank PPP data for {fallback_year} (fallback)..." + Fore.RESET)
    fallback = fetch_ppp_data(fallback_year)
    print(Fore.GREEN + f"  {len(fallback)} countries found for {fallback_year}" + Fore.RESET)

    merged = {iso2: (val, primary_year) for iso2, val in primary.items()}
    filled = 0
    for iso2, val in fallback.items():
        if iso2 not in merged:
            merged[iso2] = (val, fallback_year)
            filled += 1

    if filled:
        print(Fore.YELLOW + f"  {filled} additional countries filled from {fallback_year}" + Fore.RESET)

    return merged


def fetch_currency_codes() -> dict[str, str]:
    """Resolve ISO 4217 currency codes offline via pycountry + Babel's CLDR data.

    Returns {iso2_country_code: iso4217_currency_code}.
    For countries with multiple current currencies, the first one is used.
    """
    print(Fore.CYAN + "Resolving currency codes via pycountry/Babel..." + Fore.RESET)
    today = datetime.date.today()

    result = {}
    for country in pycountry.countries:
        currencies = get_territory_currencies(country.alpha_2, today, today, tender=True, non_tender=False)
        if currencies:
            result[country.alpha_2] = currencies[0]

    print(Fore.GREEN + f"  {len(result)} countries with currency codes" + Fore.RESET)
    return result


def fetch_country_names_from_worldbank() -> dict[str, str]:
    """Fetch ISO2 → country name mapping from World Bank country endpoint."""
    url = f"{WORLD_BANK_BASE}/country?format=json&per_page=300"
    items = _fetch_worldbank_paginated(url)
    if not items:
        raise ValueError("Unexpected World Bank country endpoint response")

    return {
        c["iso2Code"]: c["name"]
        for c in items
        if c.get("iso2Code") and c.get("region", {}).get("id") != "NA"
    }


def build_dataframe(
    ppp_data: dict[str, tuple[float, int]],
    currency_codes: dict[str, str],
    country_names: dict[str, str],
) -> pd.DataFrame:
    """Merge PPP and currency data into a DataFrame compatible with price_scaler.py."""
    rows = []
    skipped = []

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

        meal_price_native = US_REFERENCE_MEAL_USD * ppp_factor

        rows.append({
            "CountryName": name,
            "CurrencyCode": currency,
            MEAL_COLUMN: meal_price_native,
            "PPP_Factor": ppp_factor,
            "PPP_Year": year,
        })

    if skipped:
        print(Fore.YELLOW + f"Skipped {len(skipped)} entries: {skipped}" + Fore.RESET)

    return pd.DataFrame(rows)


def main():
    print(Fore.LIGHTBLUE_EX + "World Bank PPP Cost-of-Living Data Fetcher" + Fore.RESET)
    print("=" * 50)

    ppp_data = fetch_ppp_data_with_fallback(primary_year=2024, fallback_year=2023)
    currency_codes = fetch_currency_codes()

    print(Fore.CYAN + "Fetching country names from World Bank..." + Fore.RESET)
    country_names = fetch_country_names_from_worldbank()
    print(Fore.GREEN + f"  {len(country_names)} country names loaded" + Fore.RESET)

    df = build_dataframe(ppp_data, currency_codes, country_names)

    print(Fore.GREEN + f"\n{len(df)} countries ready" + Fore.RESET)

    output_file = "cost_of_living_data.xlsx"
    df.to_excel(output_file, index=False)

    print(Fore.GREEN + f"Saved to {output_file}" + Fore.RESET)
    print(Fore.CYAN + f"Shape: {df.shape}" + Fore.RESET)
    print(df[["CountryName", "CurrencyCode", "PPP_Factor", "PPP_Year"]].head(10).to_string(index=False))


if __name__ == "__main__":
    main()
