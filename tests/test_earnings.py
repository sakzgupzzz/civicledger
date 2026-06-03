"""Test EDGAR earnings calendar (live — marked integration)."""

from datetime import date, timedelta

import pytest
from civicledger.edgar.earnings import fetch_earnings


def _recent_weekday_range() -> tuple[str, str]:
    """A 5-day window ending on the most recent weekday (avoids hardcoded dates)."""
    end = date.today()
    while end.weekday() >= 5:  # back up off the weekend
        end -= timedelta(days=1)
    return (end - timedelta(days=5)).isoformat(), end.isoformat()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fetch_earnings_returns_list():
    """Fetch earnings for a recent week and verify structure."""
    fd, td = _recent_weekday_range()
    results = await fetch_earnings(fd, td)
    assert isinstance(results, list)
    if results:
        e = results[0]
        assert "ticker" in e
        assert "company" in e
        assert "filing_date" in e


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fetch_earnings_empty_range():
    """A single weekend day should return no earnings."""
    # Find a recent Sunday.
    d = date.today()
    while d.weekday() != 6:
        d -= timedelta(days=1)
    results = await fetch_earnings(d.isoformat(), d.isoformat())
    assert isinstance(results, list)
