"""Offline tests for FRED economic calendar (httpx mocked)."""

import pytest

from civicledger.economic import fred
from civicledger.economic.fred import FredApiKeyMissing, fetch_economic_events


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeClient:
    """Returns a CPI (release 10) release-date inside whatever month is queried."""

    def __init__(self, *a, **k):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None):
        params = params or {}
        start = params["realtime_start"]  # YYYY-MM-01
        year, month, _ = start.split("-")
        self.calls.append((year, month))
        # one tracked release (CPI=10) on the 12th of the queried month
        return _FakeResp({"release_dates": [
            {"release_id": 10, "date": f"{year}-{month}-12"},
            {"release_id": 99999, "date": f"{year}-{month}-13"},  # untracked -> dropped
        ]})


@pytest.mark.asyncio
async def test_missing_key_raises(monkeypatch):
    monkeypatch.setattr(fred.get_settings(), "fred_api_key", None, raising=False)
    with pytest.raises(FredApiKeyMissing):
        await fetch_economic_events("2026-02-01", "2026-02-28")


@pytest.mark.asyncio
async def test_multi_month_range_queries_each_month(monkeypatch):
    settings = fred.get_settings()
    monkeypatch.setattr(settings, "fred_api_key", "test-key", raising=False)
    fake = _FakeClient()
    monkeypatch.setattr(fred.httpx, "AsyncClient", lambda *a, **k: fake)

    events = await fetch_economic_events("2026-02-01", "2026-04-30")

    # Three months queried (Feb, Mar, Apr) -> three CPI events, none dropped.
    # (A single-month implementation would have returned only February.)
    assert {c[1] for c in fake.calls} == {"02", "03", "04"}
    cpi = [e for e in events if e["name"].startswith("Consumer Price")]
    assert len(cpi) == 3
    assert [e["date"] for e in cpi] == ["2026-02-12", "2026-03-12", "2026-04-12"]
    # Untracked release id was filtered out.
    assert all(e["source"] == "FRED" for e in events)


@pytest.mark.asyncio
async def test_range_filter_excludes_out_of_window(monkeypatch):
    settings = fred.get_settings()
    monkeypatch.setattr(settings, "fred_api_key", "test-key", raising=False)
    fake = _FakeClient()
    monkeypatch.setattr(fred.httpx, "AsyncClient", lambda *a, **k: fake)

    # Window ends on the 10th, before the fake's 12th -> February event excluded.
    events = await fetch_economic_events("2026-02-01", "2026-02-10")
    assert events == []
