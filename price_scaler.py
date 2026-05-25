#!/usr/bin/env python3
"""
Price Scaler - Scales USD prices based on cost of living data with exchange rates and purchasing power
Uses "Meal for 2 People, Mid-range Restaurant, Three-course" as the reference metric
"""

import pandas as pd
import numpy as np
import requests
import json
from typing import Dict, List, Tuple, Optional
from datetime import datetime
import time


class PriceScaler:
    def __init__(self, excel_file: str = "cost_of_living_data.xlsx"):
        """
        Initialize the PriceScaler with cost of living data

        Args:
            excel_file: Path to the Excel file containing cost of living data
        """
        self.excel_file = excel_file
        self.df = None
        self.meal_column = "Meal for 2 People, Mid-range Restaurant, Three-course"
        self.reference_country = "United States"  # Default reference country
        self.exchange_rates = {}
        self.scaling_factors = {}
        self.usd_amount = None

        self.load_data()
        self.prepare_data()
        self.fetch_exchange_rates()
        self.calculate_scaling_factors()

    def load_data(self) -> None:
        """Load the cost of living data from Excel file"""
        try:
            self.df = pd.read_excel(self.excel_file)
            print(f"✓ Loaded data for {len(self.df)} countries")
        except FileNotFoundError:
            raise FileNotFoundError(
                f"Excel file '{self.excel_file}' not found. Please ensure the file exists."
            )
        except Exception as e:
            raise RuntimeError(f"Error loading Excel file: {e}") from e

    def prepare_data(self) -> None:
        """Clean and prepare the data for calculations"""
        if self.meal_column not in self.df.columns:
            raise ValueError(f"Column '{self.meal_column}' not found in the data")

        if "CurrencyCode" not in self.df.columns:
            raise ValueError(
                "CurrencyCode column not found. Please run the updated cost-of-living.py script first."
            )

        # Remove countries with invalid meal prices
        valid_data = self.df.dropna(subset=[self.meal_column])
        print(
            f"✓ Valid data for {len(valid_data)} countries (removed {len(self.df) - len(valid_data)} with invalid data)"
        )

        self.df = valid_data.reset_index(drop=True)

    # Currencies pegged 1:1 to USD not available in the free exchangerate-api tier.
    USD_PEGGED = {"BSD", "PAB"}

    def fetch_exchange_rates(self) -> None:
        """Fetch current exchange rates from a free API"""
        print("🔄 Fetching exchange rates...")

        # Get unique currency codes
        currencies = self.df["CurrencyCode"].unique().tolist()

        # Add USD if not present
        if "USD" not in currencies:
            currencies.append("USD")

        # Use exchangerate-api.com (free tier allows 1500 requests/month)
        base_url = "https://api.exchangerate-api.com/v4/latest/USD"

        try:
            response = requests.get(base_url, timeout=10)
            response.raise_for_status()
            data = response.json()

            # Extract rates
            rates = data.get("rates", {})
            if not rates:
                raise ValueError("Exchange rate API returned empty or missing rates — unexpected response format")

            # Store exchange rates (USD to other currencies)
            for currency in currencies:
                if currency in rates:
                    self.exchange_rates[currency] = rates[currency]
                else:
                    # If currency not found, try to get it individually
                    self.exchange_rates[currency] = self.get_individual_rate(currency)

            # Apply 1:1 USD pegs for currencies not in the API
            for curr in self.USD_PEGGED:
                if curr not in self.exchange_rates:
                    self.exchange_rates[curr] = 1.0

            # Check if we have all required currencies
            missing_currencies = [
                curr
                for curr in currencies
                if curr not in self.exchange_rates
            ]
            if missing_currencies:
                raise RuntimeError(
                    f"Could not fetch exchange rates for currencies: {missing_currencies}"
                )

            print(f"✓ Fetched exchange rates for {len(self.exchange_rates)} currencies")
        except RuntimeError as e:
            print(f"❌ Error fetching exchange rates: {e}")
            raise
        except Exception as e:
            print(f"❌ Error fetching exchange rates: {e}")
            raise RuntimeError(
                f"Failed to fetch exchange rates: {e}. Please check your internet connection and try again."
            ) from e

    def get_individual_rate(self, currency: str) -> float:
        """Get individual exchange rate for a currency"""
        try:
            response = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=10)
            response.raise_for_status()
            rate = response.json().get("rates", {}).get(currency)
            if rate is None:
                raise ValueError(f"Currency {currency} not found in exchange rate API response")
            return rate
        except Exception as e:
            raise RuntimeError(f"Could not fetch exchange rate for {currency}: {e}") from e

    def calculate_scaling_factors(self) -> None:
        """Calculate scaling factors based on exchange rates and purchasing power"""
        print("🔄 Calculating scaling factors...")

        # Get US meal price in USD
        us_mask = self.df["CountryName"] == self.reference_country
        if not us_mask.any():
            print(f"Warning: Reference country '{self.reference_country}' not found.")
            return

        us_meal_price_usd = self.df[us_mask][self.meal_column].iloc[0]
        print(f"✓ US reference meal price: ${us_meal_price_usd:.2f}")

        # Calculate scaling factors for each country
        for idx, row in self.df.iterrows():
            country = row["CountryName"]
            currency_code = row["CurrencyCode"]
            meal_price_native = row[self.meal_column]

            if pd.isna(meal_price_native):
                continue

            # Step 1: Convert meal price from native currency to USD
            exchange_rate = self.exchange_rates.get(currency_code, 1.0)
            meal_price_usd = meal_price_native / exchange_rate

            # Step 2: Calculate purchasing power ratio (capped at 1.0)
            purchasing_power_ratio = max(us_meal_price_usd / meal_price_usd, 1.0)

            # Step 3: Calculate final scaling factor
            # If purchasing power is high (cheaper country), scaling factor should be low
            # If purchasing power is low (expensive country), scaling factor should be high
            scaling_factor = (
                1.0 / purchasing_power_ratio if purchasing_power_ratio > 0 else 1.0
            )

            self.scaling_factors[country] = {
                "scaling_factor": scaling_factor,
                "meal_price_native": meal_price_native,
                "meal_price_usd": meal_price_usd,
                "exchange_rate": exchange_rate,
                "purchasing_power_ratio": purchasing_power_ratio,
                "currency_code": currency_code,
            }

        print(f"✓ Calculated scaling factors for {len(self.scaling_factors)} countries")

    def apply_smart_pricing(self, price: float) -> float:
        """
        Apply smart pricing logic to make prices more appealing

        Args:
            price: Original price

        Returns:
            Price with smart pricing applied
        """
        if price <= 0:
            return price

        # For prices under $1, snap up to nearest standard price tier ending in .x9
        if price < 1.0:
            cents = int(price * 100)
            if cents >= 95:
                return 0.99
            elif cents >= 90:
                return 0.95
            elif cents >= 85:
                return 0.90
            elif cents >= 80:
                return 0.85
            elif cents >= 70:
                return 0.79
            elif cents >= 60:
                return 0.69
            elif cents >= 50:
                return 0.59
            elif cents >= 40:
                return 0.49
            elif cents >= 30:
                return 0.39
            elif cents >= 20:
                return 0.29
            elif cents >= 10:
                return 0.19
            else:
                return 0.09

        # For prices $1-$10, use .99, .95, .90, .85, .80
        elif price < 10.0:
            dollars = int(price)
            cents = int((price - dollars) * 100)

            if cents >= 50:
                return dollars + 0.99
            else:
                return (dollars - 1) + 0.99

        # For prices $10-$100, use .99, .95, .90, .85, .80, .75, .70, .65, .60, .55, .50
        elif price < 100.0:
            dollars = int(price)
            cents = int((price - dollars) * 100)

            if cents >= 50:
                return dollars + 0.99
            else:
                return (dollars - 1) + 0.99

        # For prices $100+, round to nearest 5 or 10
        else:
            if price < 1000:
                # Round to nearest 5
                return round(price / 5) * 5
            else:
                # Round to nearest 10
                return round(price / 10) * 10

    def get_tax_rate(self, country: str) -> float:
        """
        Get tax rate for a specific country

        Args:
            country: Country name

        Returns:
            Tax rate as a decimal (e.g., 0.20 for 20%)
        """
        # Common VAT/GST rates by country (as of 2024)
        tax_rates = {
            # European Union countries (VAT)
            "Austria": 0.20,
            "Belgium": 0.21,
            "Bulgaria": 0.20,
            "Croatia": 0.25,
            "Cyprus": 0.19,
            "Czech Republic": 0.21,
            "Denmark": 0.25,
            "Estonia": 0.20,
            "Finland": 0.24,
            "France": 0.20,
            "Germany": 0.19,
            "Greece": 0.24,
            "Hungary": 0.27,
            "Ireland": 0.23,
            "Italy": 0.22,
            "Latvia": 0.21,
            "Lithuania": 0.21,
            "Luxembourg": 0.17,
            "Malta": 0.18,
            "Netherlands": 0.21,
            "Poland": 0.23,
            "Portugal": 0.23,
            "Romania": 0.19,
            "Slovakia": 0.20,
            "Slovenia": 0.22,
            "Spain": 0.21,
            "Sweden": 0.25,
            # Other European countries
            "United Kingdom": 0.20,
            "Switzerland": 0.077,
            "Norway": 0.25,
            "Iceland": 0.24,
            "Liechtenstein": 0.077,
            # North America
            "United States": 0.0,  # No federal VAT, varies by state
            "Canada": 0.05,  # GST, varies by province
            "Mexico": 0.16,
            # Asia-Pacific
            "Australia": 0.10,
            "New Zealand": 0.15,
            "Japan": 0.10,
            "South Korea": 0.10,
            "Singapore": 0.07,
            "Hong Kong": 0.0,
            "Taiwan": 0.05,
            "Thailand": 0.07,
            "Malaysia": 0.06,
            "Indonesia": 0.11,
            "Philippines": 0.12,
            "Vietnam": 0.10,
            "India": 0.18,
            "China": 0.13,
            "Pakistan": 0.17,
            # Middle East & Africa
            "United Arab Emirates": 0.05,
            "Saudi Arabia": 0.15,
            "Qatar": 0.0,
            "Kuwait": 0.0,
            "Bahrain": 0.10,
            "Oman": 0.05,
            "Israel": 0.17,
            "Turkey": 0.20,
            "Egypt": 0.14,
            "South Africa": 0.15,
            "Nigeria": 0.075,
            "Kenya": 0.16,
            # South America
            "Brazil": 0.17,
            "Argentina": 0.21,
            "Chile": 0.19,
            "Colombia": 0.19,
            "Peru": 0.18,
            "Uruguay": 0.22,
            # Other major countries
            "Russia": 0.20,
            "Ukraine": 0.20,
            "Belarus": 0.20,
            "Kazakhstan": 0.12,
            "Uzbekistan": 0.12,
        }

        # Return tax rate for the country, default to 0.15 (15%) if not found
        return tax_rates.get(country, 0.15)

    def scale_price(self, usd_amount: float) -> pd.DataFrame:
        """
        Scale a USD amount based on all countries' cost of living

        Args:
            usd_amount: Original price in USD

        Returns:
            DataFrame with scaled prices for all countries
        """
        self.usd_amount = usd_amount

        results = []

        for country, data in self.scaling_factors.items():
            scaled_price = usd_amount * data["scaling_factor"]
            scaled_price_native = scaled_price * data["exchange_rate"]

            # Apply taxes after scaling but before smart pricing
            tax_rate = self.get_tax_rate(country)
            taxed_price_native = scaled_price_native * (1 + tax_rate)

            # Apply smart pricing to the taxed price
            smart_price_native = self.apply_smart_pricing(taxed_price_native)
            smart_price_usd = smart_price_native / data["exchange_rate"]

            results.append(
                {
                    "Country": country,
                    "Currency_Code": data["currency_code"],
                    "Original_USD_Amount": usd_amount,
                    "Scaled_Price_USD": scaled_price,
                    "Scaled_Price_Native": scaled_price_native,
                    "Taxed_Price_Native": taxed_price_native,
                    "Tax_Rate": tax_rate,
                    "Smart_Price_Native": smart_price_native,
                    "Smart_Price_USD": smart_price_usd,
                    "Scaling_Factor": data["scaling_factor"],
                    "Meal_Price_Native": data["meal_price_native"],
                    "Meal_Price_USD": data["meal_price_usd"],
                    "Exchange_Rate": data["exchange_rate"],
                    "Purchasing_Power_Ratio": data["purchasing_power_ratio"],
                }
            )

        return pd.DataFrame(results)

    def save_results(self, usd_amount: float) -> str:
        """
        Scale price and save results to Excel file

        Args:
            usd_amount: Original price in USD

        Returns:
            Path to the saved file
        """
        output_file = "price_scaled.xlsx"

        # Scale the price
        results_df = self.scale_price(usd_amount)

        # Sort by scaled price (descending)
        results_df = results_df.sort_values("Scaled_Price_USD", ascending=False)

        # Create Excel file with main results sheet only
        with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
            # Main results sheet
            results_df.to_excel(writer, sheet_name="Price_Scaling_Results", index=False)

        print(f"✓ Results saved to: {output_file}")
        return output_file


def main():
    """Main function for interactive use"""
    print("🌍 Price Scaler - Cost of Living Calculator with Exchange Rates")
    print("=" * 70)

    try:
        # Initialize the scaler
        scaler = PriceScaler()

        # Ask for USD amount
        usd_amount = float(input("Enter USD amount to scale: $"))

        # Save results
        saved_file = scaler.save_results(usd_amount)

        print(f"\n✅ Results saved to: {saved_file}")

    except ValueError:
        print("❌ Invalid amount. Please enter a valid number.")
    except Exception as e:
        print(f"❌ Error: {e}")


if __name__ == "__main__":
    main()
