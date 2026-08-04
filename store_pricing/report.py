"""Failure reporting, shared by apply and offer commands."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


def make_failure(platform: str, country, country_code, price, currency, reason) -> dict:
    return {
        "platform": platform,
        "country": country,
        "country_code": country_code,
        "price": price,
        "currency": currency,
        "reason": reason,
    }


def write_failure_report(failures: list[dict]) -> "Path | None":
    """Write price_update_failures_<timestamp>.txt. Returns the path, or None if there
    were no failures to report."""
    if not failures:
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = Path(f"price_update_failures_{timestamp}.txt")

    google_failures = [f for f in failures if f["platform"] == "Google Play"]
    apple_failures = [f for f in failures if f["platform"] == "Apple App Store"]
    stripe_failures = [f for f in failures if f["platform"] == "Stripe"]

    with path.open("w") as f:
        f.write("PRICE UPDATE FAILURE REPORT\n")
        f.write("=" * 50 + "\n")
        f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Total failures: {len(failures)}\n\n")

        for label, group in (
            ("GOOGLE PLAY FAILURES", google_failures),
            ("APPLE APP STORE FAILURES", apple_failures),
            ("STRIPE FAILURES", stripe_failures),
        ):
            if not group:
                continue
            f.write(f"{label}:\n")
            f.write("-" * 30 + "\n")
            for failure in group:
                f.write(f"• {failure['country']} ({failure['country_code']}) - {failure['reason']}\n")
            f.write("\n")

    return path
