"""Unit tests for civicledger.validation — pure, offline, deterministic."""

from datetime import date

import pytest

from civicledger.validation import (
    ValidationError,
    iter_months,
    normalize_8k_item,
    normalize_date,
    normalize_date_range,
    normalize_limit,
    normalize_ticker,
    normalize_year,
)


@pytest.mark.parametrize("raw,expected", [
    (" aapl ", "AAPL"),
    ("brk.a", "BRK.A"),
    ("BRK-B", "BRK-B"),
    ("x", "X"),
])
def test_normalize_ticker_ok(raw, expected):
    assert normalize_ticker(raw) == expected


@pytest.mark.parametrize("bad", ["", "   ", "123", "TOOLONGSYM", "AA PL", "$$$"])
def test_normalize_ticker_bad(bad):
    with pytest.raises(ValidationError):
        normalize_ticker(bad)


def test_normalize_date_ok():
    assert normalize_date("2026-03-17") == "2026-03-17"


@pytest.mark.parametrize("bad", ["", "2026-13-40", "03/17/2026", "2026-3-7x", "not-a-date"])
def test_normalize_date_bad(bad):
    with pytest.raises(ValidationError):
        normalize_date(bad)


def test_date_range_defaults():
    fd, td = normalize_date_range(None, None, default_lookback_days=7, today=date(2026, 6, 2))
    assert fd == "2026-05-26"
    assert td == "2026-06-02"


def test_date_range_order_enforced():
    with pytest.raises(ValidationError):
        normalize_date_range("2026-06-10", "2026-06-01")


def test_date_range_max_span():
    with pytest.raises(ValidationError):
        normalize_date_range("2026-01-01", "2026-12-31", max_span_days=92)


def test_normalize_limit_clamps():
    assert normalize_limit(99999, maximum=500) == 500
    assert normalize_limit(-5, default=50) == 50
    assert normalize_limit("nope", default=10) == 10
    assert normalize_limit(25, maximum=500) == 25


def test_normalize_8k_item():
    assert normalize_8k_item("5.02") == "5.02"
    assert normalize_8k_item(None) is None
    assert normalize_8k_item("  ") is None
    with pytest.raises(ValidationError):
        normalize_8k_item("5.99")


def test_normalize_year():
    assert normalize_year(None, today=date(2026, 6, 2)) == 2026
    assert normalize_year(2020) == 2020
    with pytest.raises(ValidationError):
        normalize_year(1990)
    with pytest.raises(ValidationError):
        normalize_year(2099, today=date(2026, 6, 2))


def test_iter_months_spans_boundaries():
    assert list(iter_months("2026-02-15", "2026-04-03")) == [(2026, 2), (2026, 3), (2026, 4)]
    assert list(iter_months("2025-12-20", "2026-01-05")) == [(2025, 12), (2026, 1)]
    assert list(iter_months("2026-06-01", "2026-06-30")) == [(2026, 6)]
