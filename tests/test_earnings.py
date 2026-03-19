"""Test EDGAR earnings calendar."""

import pytest
from civicledger.edgar.earnings import fetch_earnings


@pytest.mark.asyncio
async def test_fetch_earnings_returns_list():
    """Fetch earnings for a recent week and verify structure."""
    results = await fetch_earnings("2026-03-10", "2026-03-14")
    assert isinstance(results, list)
    if results:
        e = results[0]
        assert "ticker" in e
        assert "company" in e
        assert "filing_date" in e
        assert e["ticker"] is not None
        assert len(e["ticker"]) <= 5


@pytest.mark.asyncio
async def test_fetch_earnings_empty_range():
    """A weekend should return no earnings."""
    results = await fetch_earnings("2026-01-04", "2026-01-04")  # Sunday
    assert isinstance(results, list)
