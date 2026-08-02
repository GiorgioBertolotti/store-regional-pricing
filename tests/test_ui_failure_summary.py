"""failure_summary_table() groups failures by platform + reason category instead of
listing every country as its own row. A subscription with a lot of legacy pricing debt can
produce 100+ individual failures that boil down to a handful of underlying reasons -
without grouping, that reads as noise rather than something actionable."""

from store_pricing.ui import failure_summary_table


def _failure(platform, country, code, reason):
    return {"platform": platform, "country": country, "country_code": code, "price": None, "currency": None, "reason": reason}


def test_same_reason_failures_are_grouped_into_one_row():
    failures = [
        _failure("Google Play", "Bahamas, The", "BS", "Currency mismatch: Google expects USD, we have BSD (couldn't auto-convert - no USD price or exchange rate available)"),
        _failure("Google Play", "Haiti", "HT", "Currency mismatch: Google expects USD, we have HTG (couldn't auto-convert - no USD price or exchange rate available)"),
    ]
    table = failure_summary_table(failures)

    assert table.row_count == 1
    rendered = _rendered(table)
    assert "Bahamas, The (BS)" in rendered
    assert "Haiti (HT)" in rendered
    assert "Currency mismatch" in rendered


def test_different_reasons_get_separate_rows():
    failures = [
        _failure("Google Play", "Palau", "PW", "Region not found in Google Play configuration"),
        _failure("Apple App Store", "Haiti", "HTI", "No price points available with currency HTG"),
    ]
    table = failure_summary_table(failures)

    assert table.row_count == 2


def test_long_country_lists_are_truncated_with_a_count():
    failures = [_failure("Google Play", f"Country{i}", f"C{i}", "Region not found in Google Play configuration") for i in range(12)]
    table = failure_summary_table(failures)

    rendered = _rendered(table)
    assert "+4 more" in rendered


def test_title_reports_total_and_distinct_reason_counts():
    failures = [
        _failure("Google Play", "Palau", "PW", "Region not found in Google Play configuration"),
        _failure("Google Play", "China", "CN", "Region not found in Google Play configuration"),
        _failure("Apple App Store", "Haiti", "HTI", "No price points available with currency HTG"),
    ]
    table = failure_summary_table(failures)

    assert table.title == "3 failures, 2 distinct reason(s)"


def _rendered(table) -> str:
    from io import StringIO
    from rich.console import Console

    buf = StringIO()
    Console(file=buf, width=160).print(table)
    return buf.getvalue()
