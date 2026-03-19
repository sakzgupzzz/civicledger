"""SEC EDGAR Earnings Calendar — from 8-K Item 2.02 filings.

Item 2.02 = "Results of Operations and Financial Condition" — the
standardized way companies announce earnings. Single EFTS API call
covers any date range.

Source: https://efts.sec.gov/LATEST/search-index
Public domain. No API key required.
"""

import asyncio
import re
from typing import Any, Dict, List

from loguru import logger

from civicledger.edgar._client import efts_search

TICKER_RE = re.compile(r"\(([A-Z]{1,5})\)")


async def fetch_earnings(from_date: str, to_date: str) -> List[Dict[str, Any]]:
    """Fetch earnings announcements from SEC EDGAR EFTS.

    Searches for 8-K filings with Item 2.02 in the given date range.
    Returns list of {ticker, company, filing_date, cik}.
    """
    all_earnings: List[Dict[str, Any]] = []
    page = 0

    while True:
        data = await efts_search(
            query='"Item 2.02"',
            forms="8-K",
            start_date=from_date,
            end_date=to_date,
            page=page,
            size=200,
        )

        if not data:
            break

        hits = data.get("hits", {}).get("hits", [])
        total = data.get("hits", {}).get("total", {}).get("value", 0)

        for h in hits:
            s = h.get("_source", {})
            items = s.get("items", [])
            if "2.02" not in items:
                continue

            filing_date = s.get("file_date")
            display_names = s.get("display_names", [])
            ciks = s.get("ciks", [])

            for i, name in enumerate(display_names):
                ticker_match = TICKER_RE.search(name or "")
                ticker = ticker_match.group(1) if ticker_match else None
                company = (name or "").split("(")[0].strip()
                cik = int(ciks[i]) if i < len(ciks) else None

                if not ticker:
                    continue

                all_earnings.append({
                    "ticker": ticker,
                    "company": company,
                    "filing_date": filing_date,
                    "cik": cik,
                })

        fetched = (page + 1) * 200
        if fetched >= total or not hits:
            break
        page += 1
        await asyncio.sleep(0.12)

    # Dedupe by (ticker, date)
    seen: set = set()
    deduped: List[Dict[str, Any]] = []
    for e in all_earnings:
        key = (e["ticker"], e["filing_date"])
        if key not in seen:
            seen.add(key)
            deduped.append(e)

    deduped.sort(key=lambda x: (x.get("filing_date", ""), x.get("ticker", "")))
    logger.info(f"EDGAR earnings: {len(deduped)} announcements for {from_date} to {to_date}")
    return deduped
