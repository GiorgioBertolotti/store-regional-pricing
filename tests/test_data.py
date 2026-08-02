import datetime
import json

import pandas as pd

from store_pricing import data as data_mod


def test_ppp_fallback_fills_gaps_from_earlier_years(monkeypatch):
    current_year = datetime.date.today().year
    by_year = {
        current_year: {"US": 1.0, "NO": 12.5},              # missing DE this year
        current_year - 1: {"US": 1.0, "NO": 12.0, "DE": 0.9},
        current_year - 2: {"US": 1.0},
    }
    monkeypatch.setattr(data_mod, "fetch_ppp_data", lambda year: by_year.get(year, {}))

    merged = data_mod.fetch_ppp_data_with_fallback(ppp_year="auto", max_lookback=3)

    # This year's values win where present...
    assert merged["US"] == (1.0, current_year)
    assert merged["NO"] == (12.5, current_year)
    # ...and DE is filled from the first earlier year that has it.
    assert merged["DE"] == (0.9, current_year - 1)


def test_ppp_fallback_pins_a_specific_start_year(monkeypatch):
    by_year = {2023: {"US": 1.0}, 2022: {"US": 1.0, "FR": 0.95}}
    monkeypatch.setattr(data_mod, "fetch_ppp_data", lambda year: by_year.get(year, {}))

    merged = data_mod.fetch_ppp_data_with_fallback(ppp_year="2023", max_lookback=2)

    assert merged["US"] == (1.0, 2023)
    assert merged["FR"] == (0.95, 2022)


def test_refresh_country_codes_is_merge_only(tmp_path, monkeypatch):
    cost_of_living_path = tmp_path / "cost_of_living_data.xlsx"
    country_codes_path = tmp_path / "country_codes.json"

    pd.DataFrame([{"CountryName": "Norway"}, {"CountryName": "France"}]).to_excel(cost_of_living_path, index=False)

    # Pre-existing entry for a country no longer present in this run's input - must survive.
    country_codes_path.write_text(json.dumps({"Germany": {"alpha2": "DE", "alpha3": "DEU"}}))

    monkeypatch.setattr(data_mod, "fetch_worldbank_country_names", lambda: {"NO": "Norway", "FR": "France"})
    monkeypatch.setattr(data_mod, "build_iso2_to_iso3", lambda: {"NO": "NOR", "FR": "FRA"})

    result = data_mod.refresh_country_codes(cost_of_living_path, country_codes_path)

    assert result["Norway"] == {"alpha2": "NO", "alpha3": "NOR"}
    assert result["France"] == {"alpha2": "FR", "alpha3": "FRA"}
    # Stale entry kept, not dropped.
    assert result["Germany"] == {"alpha2": "DE", "alpha3": "DEU"}

    on_disk = json.loads(country_codes_path.read_text())
    assert on_disk == result


def test_refresh_country_codes_skips_unresolvable_countries(tmp_path, monkeypatch, capsys):
    cost_of_living_path = tmp_path / "cost_of_living_data.xlsx"
    country_codes_path = tmp_path / "country_codes.json"

    pd.DataFrame([{"CountryName": "Nowhereland"}]).to_excel(cost_of_living_path, index=False)
    monkeypatch.setattr(data_mod, "fetch_worldbank_country_names", lambda: {})
    monkeypatch.setattr(data_mod, "build_iso2_to_iso3", lambda: {})

    result = data_mod.refresh_country_codes(cost_of_living_path, country_codes_path)

    assert result == {}
    assert "Skipped" in capsys.readouterr().out
