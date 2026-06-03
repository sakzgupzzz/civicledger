"""FRED Economic Calendar — release dates for tracked macro indicators.

Source: https://fred.stlouisfed.org/
Free API key required: https://fred.stlouisfed.org/docs/api/api_key.html
Public domain (US government data). Attribution required.

Attribution: "This product uses the FRED API but is not endorsed or certified
by the Federal Reserve Bank of St. Louis."
"""

from calendar import monthrange
from typing import Any, Dict, List

import httpx
from loguru import logger

from civicledger.config import get_settings
from civicledger.validation import iter_months


class FredApiKeyMissing(RuntimeError):
    """Raised when a FRED-backed call is made without an API key configured."""

# FRED release IDs for tracked macro indicators
TRACKED_RELEASES = {
    10: {
        "name": "Consumer Price Index (CPI)",
        "impact": "high",
        "description": "Measures the average change in prices paid by urban consumers. The most closely watched inflation gauge.",
    },
    50: {
        "name": "Employment Situation (NFP)",
        "impact": "high",
        "description": "Non-Farm Payrolls — jobs added/lost, unemployment rate, and wage growth.",
    },
    21: {
        "name": "GDP",
        "impact": "high",
        "description": "Gross Domestic Product — the broadest measure of economic output.",
    },
    19: {
        "name": "FOMC / Fed Funds Rate",
        "impact": "high",
        "description": "The Federal Open Market Committee sets the federal funds rate target.",
    },
    46: {
        "name": "Producer Price Index (PPI)",
        "impact": "high",
        "description": "Measures wholesale price changes before they reach consumers.",
    },
    13: {
        "name": "Retail Sales",
        "impact": "medium",
        "description": "Monthly measure of total receipts at retail stores. Consumer spending drives ~70% of GDP.",
    },
    53: {
        "name": "ISM Manufacturing PMI",
        "impact": "medium",
        "description": "Purchasing Managers' Index for manufacturing. Above 50 = expansion.",
    },
    54: {
        "name": "ISM Services PMI",
        "impact": "medium",
        "description": "Purchasing Managers' Index for services (~80% of US economy).",
    },
    11: {
        "name": "Consumer Confidence",
        "impact": "medium",
        "description": "Survey-based measure of how optimistic consumers feel about the economy.",
    },
    31: {
        "name": "Durable Goods Orders",
        "impact": "medium",
        "description": "New orders for long-lasting goods. Indicator of business investment.",
    },
    32: {
        "name": "Housing Starts & Permits",
        "impact": "medium",
        "description": "New residential construction projects and building permits issued.",
    },
    17: {
        "name": "Industrial Production",
        "impact": "low",
        "description": "Output from manufacturing, mining, and utilities.",
    },
    22: {
        "name": "Existing Home Sales",
        "impact": "low",
        "description": "Completed sales of previously owned homes.",
    },
    83: {
        "name": "New Home Sales",
        "impact": "low",
        "description": "Sales of newly built homes. Sensitive to mortgage rates.",
    },
}


async def fetch_economic_events(
    from_date: str,
    to_date: str,
) -> List[Dict[str, Any]]:
    """Fetch upcoming FRED release dates for tracked macro indicators.

    Uses the /fred/releases/dates endpoint with a single API call per month,
    then filters to our tracked releases.

    Args:
        from_date: Start date (YYYY-MM-DD)
        to_date: End date (YYYY-MM-DD)

    Returns list of {name, date, impact, description, source}.
    """
    settings = get_settings()
    api_key = settings.fred_api_key
    if not api_key:
        raise FredApiKeyMissing(
            "FRED API key not set. Get a free key at "
            "https://fred.stlouisfed.org/docs/api/api_key.html and set "
            "CIVICLEDGER_FRED_API_KEY."
        )

    # Query each month spanning the range. The FRED releases/dates endpoint
    # filters by realtime window, so a single month query would silently drop
    # events in any other month the range touches.
    tracked_ids = set(TRACKED_RELEASES.keys())
    events: List[Dict[str, Any]] = []
    seen: set = set()

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            for year, month in iter_months(from_date, to_date):
                last_day = monthrange(year, month)[1]
                resp = await client.get(
                    "https://api.stlouisfed.org/fred/releases/dates",
                    params={
                        "api_key": api_key,
                        "file_type": "json",
                        "realtime_start": f"{year}-{month:02d}-01",
                        "realtime_end": f"{year}-{month:02d}-{last_day:02d}",
                        "include_release_dates_with_no_data": "true",
                        "sort_order": "asc",
                        "limit": 1000,
                    },
                )
                resp.raise_for_status()
                for d in resp.json().get("release_dates", []):
                    rid = d.get("release_id")
                    event_date = d.get("date", "")
                    if rid in tracked_ids and from_date <= event_date <= to_date:
                        key = (rid, event_date)
                        if key in seen:
                            continue
                        seen.add(key)
                        meta = TRACKED_RELEASES[rid]
                        events.append({
                            "name": meta["name"],
                            "date": event_date,
                            "impact": meta["impact"],
                            "description": meta["description"],
                            "source": "FRED",
                        })

        events.sort(key=lambda e: e["date"])
        logger.info(f"FRED events: {len(events)} tracked releases for {from_date} to {to_date}")
        return events

    except FredApiKeyMissing:
        raise
    except Exception as e:
        logger.warning(f"FRED releases fetch failed: {e}")
        return []
