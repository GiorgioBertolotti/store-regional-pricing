"""
Regenerates country_codes.json from the World Bank and restcountries.com APIs.

Reads CountryName values from cost_of_living_data.xlsx (produced by cost-of-living.py),
resolves the corresponding ISO alpha-2 and alpha-3 codes, and writes country_codes.json
in the format expected by subscription_price_applier.py:

  {
    "Country Name": {"alpha2": "XX", "alpha3": "XXX"},
    ...
  }

Run this script whenever new countries appear in cost_of_living_data.xlsx
(e.g. after a World Bank dataset refresh) or when country_codes.json is missing.
"""

from __future__ import annotations

import json
import sys

import pandas as pd
import requests
import colorama
from colorama import Fore

colorama.init()

WORLD_BANK_BASE = "https://api.worldbank.org/v2"
RESTCOUNTRIES_URL = "https://restcountries.com/v3.1/all?fields=name,cca2,cca3"
INPUT_FILE = "cost_of_living_data.xlsx"
OUTPUT_FILE = "country_codes.json"


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


def fetch_worldbank_iso2_to_name() -> dict[str, str]:
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


def fetch_restcountries_iso2_to_iso3() -> dict[str, str]:
    """Return {iso2: iso3} from restcountries.com."""
    print(Fore.CYAN + "Fetching ISO alpha-3 codes from restcountries.com..." + Fore.RESET)
    response = requests.get(RESTCOUNTRIES_URL, timeout=30)
    response.raise_for_status()
    countries = response.json()

    return {c["cca2"]: c["cca3"] for c in countries if c.get("cca2") and c.get("cca3")}


def load_existing_codes() -> dict:
    try:
        with open(OUTPUT_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def main() -> None:
    print(Fore.LIGHTBLUE_EX + "Country Code Updater" + Fore.RESET)
    print("=" * 50)

    try:
        df = pd.read_excel(INPUT_FILE)
    except FileNotFoundError:
        print(Fore.RED + f"❌ {INPUT_FILE} not found. Run cost-of-living.py first." + Fore.RESET)
        sys.exit(1)

    if "CountryName" not in df.columns:
        print(Fore.RED + "❌ CountryName column missing in input file." + Fore.RESET)
        sys.exit(1)

    country_names_in_file: list[str] = df["CountryName"].dropna().unique().tolist()
    print(Fore.GREEN + f"  {len(country_names_in_file)} countries in {INPUT_FILE}" + Fore.RESET)

    print(Fore.CYAN + "Fetching country names from World Bank..." + Fore.RESET)
    wb_iso2_to_name = fetch_worldbank_iso2_to_name()
    # Invert: world-bank name → iso2
    wb_name_to_iso2 = {v: k for k, v in wb_iso2_to_name.items()}
    print(Fore.GREEN + f"  {len(wb_name_to_iso2)} entries from World Bank" + Fore.RESET)

    iso2_to_iso3 = fetch_restcountries_iso2_to_iso3()
    print(Fore.GREEN + f"  {len(iso2_to_iso3)} entries from restcountries.com" + Fore.RESET)

    existing = load_existing_codes()
    updated = dict(existing)
    added = []
    skipped = []

    for name in sorted(country_names_in_file):
        iso2 = wb_name_to_iso2.get(name)
        if not iso2:
            skipped.append(f"{name} (no ISO2 from World Bank)")
            continue

        iso3 = iso2_to_iso3.get(iso2)
        if not iso3:
            skipped.append(f"{name} ({iso2}, no ISO3 from restcountries)")
            continue

        entry = {"alpha2": iso2, "alpha3": iso3}
        if updated.get(name) != entry:
            updated[name] = entry
            added.append(name)

    if skipped:
        print(Fore.YELLOW + f"\nSkipped {len(skipped)} countries:" + Fore.RESET)
        for s in skipped:
            print(Fore.YELLOW + f"  - {s}" + Fore.RESET)

    with open(OUTPUT_FILE, "w") as f:
        json.dump(updated, f, indent=2, ensure_ascii=False, sort_keys=True)

    print(Fore.GREEN + f"\n✅ {OUTPUT_FILE} updated: {len(updated)} total entries" + Fore.RESET)
    if added:
        print(Fore.GREEN + f"   {len(added)} new/changed: {added[:10]}{'...' if len(added) > 10 else ''}" + Fore.RESET)
    else:
        print(Fore.CYAN + "   No changes needed." + Fore.RESET)


if __name__ == "__main__":
    main()
