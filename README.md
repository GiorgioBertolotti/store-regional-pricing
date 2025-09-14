# Price Scaler - Cost of Living Calculator with Exchange Rates

## Installation

Install required dependencies:
```bash
pip install -r requirements.txt
```

## Usage

### Step 1: Extract Cost of Living Data

First, extract the cost of living data with currency codes:

```bash
python cost-of-living.py
```

This generates `cost_of_living_data.xlsx` with native currency data.

### Step 2: Scale Prices

Run the main price scaler:

```bash
python price_scaler.py
```

When asked, input the amount of USD you want to scale the prices to, this will generate `price_scaled.xlsx` with the scaled prices.

### Step 3: Apply to App Stores

Generate a `.env` file with all the required variables (see below).  
Apply the scaled prices to your app stores:

```bash
python subscription_price_applier.py
```

## Environment Variables

```bash
# Google Play Console Configuration
GOOGLE_SERVICE_ACCOUNT_FILE=service-account-file.json
GOOGLE_PACKAGE_NAME=com.example.app
GOOGLE_SUBSCRIPTION_ID=your_subscription_id
GOOGLE_BASEPLAN_ID=base-plan

# Apple App Store Connect Configuration
APPLE_ISSUER_ID=your_issuer_id
APPLE_KEY_ID=your_api_key_id
APPLE_PRIVATE_KEY=your_private_key_content_here
APPLE_APP_ID=your_subscription_group_id
```

## How to get the environment variables

### Google Play Console Configuration

- GOOGLE_SERVICE_ACCOUNT_FILE: Service account file from Google Play Console
- GOOGLE_PACKAGE_NAME: Package name of the app
- GOOGLE_SUBSCRIPTION_ID: Subscription ID of the app
- GOOGLE_BASEPLAN_ID: Base plan ID of the app

### Apple App Store Connect Configuration

- APPLE_ISSUER_ID: Issuer ID of the app
- APPLE_KEY_ID: API key ID of the app
- APPLE_PRIVATE_KEY: Private key of the app
- APPLE_APP_ID: Subscription group ID of the app
