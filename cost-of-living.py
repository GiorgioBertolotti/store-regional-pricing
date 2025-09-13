import requests
from bs4 import BeautifulSoup
import pandas as pd
import colorama
from colorama import Fore, Back, Style
import re
import time

colorama.init()


class webScraping:

    def __init__(self) -> None:
        self.DataDict = dict()
        self.DataDict["CountryName"] = list()
        self.DataDict["CurrencyCode"] = list()

    def getCountyNameList(self, countryURL="https://www.numbeo.com/cost-of-living/"):

        res = requests.get(countryURL).text
        soup = BeautifulSoup(res, "html.parser")

        countyrlist = list()

        for a in soup.find_all("a", href=True):
            if "country_result" in a["href"]:
                countyrlist.append(a["href"].split("=")[1])

        return countyrlist

    def getCountriesCostOfLiving(self, countryURL, countryName):
        self.DataDict["CountryName"].append(countryName)
        
        res = requests.get(countryURL).text
        soup = BeautifulSoup(res, "html.parser")
        
        # Extract currency code from the select tag with id "displayCurrency"
        currency_code = self.extract_currency_code(soup)
        self.DataDict["CurrencyCode"].append(currency_code)
        
        table = soup.find("table", class_="data_wide_table")

        for row in table.find_all("tr"):
            column = row.find_all("td")
            if column != []:
                name = column[0].text.strip()
                price = column[1].text.strip()

                cleaned_price = self.clean_price(price)

                if name not in [i for i in self.DataDict.keys()]:
                    self.DataDict[name] = list()
                    self.DataDict[name].append(cleaned_price)
                else:
                    self.DataDict[name].append(cleaned_price)

    def extract_currency_code(self, soup):
        """Extract currency code from the select tag with id 'displayCurrency'"""
        try:
            select_tag = soup.find("select", {"id": "displayCurrency"})
            if select_tag:
                selected_option = select_tag.find("option", {"selected": True})
                if selected_option:
                    currency_code = selected_option.get("value", "USD")
                    return currency_code
        except Exception as e:
            print(f"Warning: Could not extract currency: {e}")
        
        # Fallback to USD if extraction fails
        return "USD"

    def clean_price(self, price_str):
        """Clean price string by removing currency symbols and keeping only numbers"""
        if pd.isna(price_str) or price_str in ["?", "N/A", "-", ""]:
            return None

        # Remove currency symbols and extra whitespace, keep only numbers and decimal points
        cleaned = re.sub(r"[^\d.,]", "", str(price_str))

        # Handle different decimal separators
        if "," in cleaned and "." in cleaned:
            # Assume comma is thousands separator
            cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            # Assume comma is decimal separator
            cleaned = cleaned.replace(",", ".")

        try:
            return float(cleaned)
        except ValueError:
            return None

    def CountryNameOperation(self, countryName):

        countryName = countryName.replace("%28", "(")
        countryName = countryName.replace("%29", ")")
        countryName = countryName.replace("+", " ")

        return countryName

    def dataMerge(self):

        countyrsList = self.getCountyNameList()
        print(
            Fore.LIGHTRED_EX,
            "Country names successfully imported (total number of countries : {})\n".format(
                len(countyrsList)
            ),
        )
        counter = 1
        for countryName in countyrsList:
            countryName_ = self.CountryNameOperation(countryName)
            url = "https://www.numbeo.com/cost-of-living/country_result.jsp?country={}".format(
                countryName
            )
            self.getCountriesCostOfLiving(url, countryName_)
            print(
                Fore.GREEN,
                "    Successfully completed ",
                Fore.MAGENTA,
                "--------------------- ",
                Fore.WHITE,
                "{}/{}".format(counter, len(countyrsList)),
                Fore.MAGENTA,
                "----",
                Fore.BLUE,
                "{}".format(countryName_),
            )

            counter += 1

        return self.DataDict

    def getDataFrame(self):
        dataDict = self.DataDict
        counterDict = dict()

        for i in dataDict:
            if str(len(dataDict[i])) not in [ky for ky in counterDict.keys()]:

                counterDict[str(len(dataDict[i]))] = 1
            else:
                counterDict[str(len(dataDict[i]))] += 1

        maxValue = max(counterDict, key=counterDict.get)

        lastDict = dict()
        for i in dataDict:
            if len(dataDict[i]) == int(maxValue):
                lastDict[i] = dataDict[i]

        print(Fore.LIGHTGREEN_EX, "\nTransactions completed successfully.", Fore.RESET)

        return pd.DataFrame(lastDict)


WS = webScraping()

try:
    WS.dataMerge()
    data = WS.getDataFrame()

    # Save to Excel file
    filename = "cost_of_living_data.xlsx"
    data.to_excel(filename, index=False)
    print(Fore.GREEN, f"Data successfully saved to {filename}", Fore.RESET)
    print(Fore.CYAN, f"DataFrame shape: {data.shape}", Fore.RESET)
    print(data.head())  # Show first few rows for verification

except Exception as e:
    print(Fore.RED, f"Error occurred during data merge: {e}", Fore.RESET)
    print(
        Fore.YELLOW, "Attempting to save available data before closing...", Fore.RESET
    )
    try:
        data = WS.getDataFrame()
        filename = "cost_of_living_data_partial.xlsx"
        data.to_excel(filename, index=False)
        print(Fore.GREEN, f"Partial data saved to {filename}", Fore.RESET)
        print(Fore.CYAN, f"DataFrame shape: {data.shape}", Fore.RESET)
        print(data.head())
    except Exception as e2:
        print(Fore.RED, f"Could not retrieve dataframe: {e2}", Fore.RESET)
        print(Fore.YELLOW, "Raw data dictionary:", Fore.RESET)
        print(WS.DataDict)
