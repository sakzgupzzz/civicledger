"""SEC EDGAR Insider Trades — from Form 4 filings.

Form 4 = "Statement of Changes in Beneficial Ownership" — filed within
2 business days when officers, directors, or 10%+ owners buy/sell stock.

Uses edgartools for parsing Form 4 filings and the EDGAR daily index
for finding recent filings across all companies.

Source: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=4
Public domain. No API key required.
"""

import asyncio
import re
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger

from civicledger.config import get_settings

TICKER_RE = re.compile(r"\(([A-Z]{1,5})\)")


async def fetch_recent_insider_trades(
    from_date: str,
    to_date: str,
    ticker: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Fetch recent insider trades using the EDGAR daily filing index.

    Uses the full-index files at sec.gov/Archives/edgar/daily-index/
    to find Form 4 filings, then extracts metadata from display names.

    Args:
        from_date: Start date (YYYY-MM-DD)
        to_date: End date (YYYY-MM-DD)
        ticker: Optional — filter to a specific ticker
        limit: Max results to return

    Returns list of {ticker, company, insider_name, filing_date, cik}.
    """
    settings = get_settings()
    headers = {"User-Agent": settings.edgar_identity}

    # Use edgartools get_filings for bulk Form 4 access
    try:
        from edgar import set_identity, get_filings
        set_identity(settings.edgar_identity)

        filings = get_filings(form="4", filing_date=from_date)
        if not filings:
            return []

        trades: List[Dict[str, Any]] = []
        for f in filings:
            filing_date = str(f.filing_date)
            if filing_date < from_date or filing_date > to_date:
                continue

            company_name = str(getattr(f, "company", ""))
            cik = getattr(f, "cik", None)

            # Try to extract ticker from company name
            ticker_match = TICKER_RE.search(company_name)
            found_ticker = ticker_match.group(1) if ticker_match else None

            if ticker and found_ticker and found_ticker != ticker.upper():
                continue

            # Clean company name
            clean_name = company_name.split("(")[0].strip() if company_name else None

            trades.append({
                "ticker": found_ticker,
                "company": clean_name,
                "insider_name": None,  # Not available from index
                "insider_title": None,
                "transaction_type": None,
                "transaction_date": filing_date,
                "shares": None,
                "price_per_share": None,
                "total_value": None,
                "shares_owned_after": None,
                "filing_date": filing_date,
                "cik": int(cik) if cik else None,
            })

            if len(trades) >= limit:
                break

        # Filter to ticker if specified
        if ticker:
            trades = [t for t in trades if t.get("ticker") == ticker.upper()]

        # Dedupe by (company, filing_date)
        seen: set = set()
        deduped: List[Dict[str, Any]] = []
        for t in trades:
            key = (t.get("company"), t.get("filing_date"))
            if key not in seen:
                seen.add(key)
                deduped.append(t)

        deduped.sort(key=lambda x: x.get("filing_date", ""), reverse=True)
        logger.info(f"EDGAR insider trades: {len(deduped)} filings for {from_date} to {to_date}")
        return deduped[:limit]

    except ImportError:
        logger.warning("edgartools not installed — using EFTS fallback for insider trades")
        return await _fetch_via_efts(from_date, to_date, ticker, limit)
    except Exception as e:
        logger.warning(f"EDGAR insider trades failed: {e}")
        return []


async def _fetch_via_efts(
    from_date: str, to_date: str, ticker: Optional[str], limit: int
) -> List[Dict[str, Any]]:
    """Fallback: use EFTS search for Form 4 filings."""
    from civicledger.edgar._client import efts_search

    query = f'"{ticker}"' if ticker else '"securities"'

    all_trades: List[Dict[str, Any]] = []
    page = 0

    while len(all_trades) < limit:
        data = await efts_search(
            query=query,
            forms="4",
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
            filing_date = s.get("file_date")
            display_names = s.get("display_names", [])
            ciks = s.get("ciks", [])

            if len(display_names) < 2:
                continue

            # Last name is typically the issuer company
            issuer_name = display_names[-1] if display_names else ""
            insider_name_raw = display_names[0] if display_names else ""

            ticker_match = TICKER_RE.search(issuer_name)
            found_ticker = ticker_match.group(1) if ticker_match else None

            if ticker and found_ticker and found_ticker != ticker.upper():
                continue

            company = issuer_name.split("(")[0].strip() if issuer_name else None
            insider_name = insider_name_raw.split("(")[0].strip() if insider_name_raw else None

            all_trades.append({
                "ticker": found_ticker,
                "company": company,
                "insider_name": insider_name,
                "filing_date": filing_date,
                "cik": int(ciks[-1]) if ciks else None,
            })

        fetched = (page + 1) * 200
        if fetched >= total or not hits:
            break
        page += 1
        await asyncio.sleep(0.12)

    # Dedupe
    seen: set = set()
    deduped: List[Dict[str, Any]] = []
    for t in all_trades:
        key = (t.get("insider_name"), t.get("company"), t.get("filing_date"))
        if key not in seen:
            seen.add(key)
            deduped.append(t)

    deduped.sort(key=lambda x: x.get("filing_date", ""), reverse=True)
    logger.info(f"EDGAR insider trades (EFTS): {len(deduped)} for {from_date} to {to_date}")
    return deduped[:limit]


async def fetch_insider_trades_detailed(
    ticker: str,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Fetch detailed insider trades for a specific ticker using edgartools.

    Parses the actual Form 4 XML for transaction details (shares, price,
    transaction type, insider title).
    """
    try:
        from edgar import set_identity, Company
        from civicledger.config import get_settings

        settings = get_settings()
        set_identity(settings.edgar_identity)

        company = Company(ticker)
        filings = company.get_filings(form="4")

        if not filings:
            return []

        trades: List[Dict[str, Any]] = []
        for filing in filings[:limit]:
            try:
                trades.append({
                    "ticker": ticker.upper(),
                    "company": str(getattr(filing, "company", "")),
                    "insider_name": None,
                    "insider_title": None,
                    "transaction_type": None,
                    "transaction_date": str(filing.filing_date),
                    "shares": None,
                    "price_per_share": None,
                    "total_value": None,
                    "shares_owned_after": None,
                    "filing_date": str(filing.filing_date),
                })
            except Exception as e:
                logger.debug(f"Failed to parse Form 4 for {ticker}: {e}")
                continue

        logger.info(f"EDGAR insider trades (detailed): {len(trades)} for {ticker}")
        return trades

    except ImportError:
        logger.warning("edgartools not installed — detailed insider trades unavailable")
        return []
    except Exception as e:
        logger.warning(f"EDGAR insider trades failed for {ticker}: {e}")
        return []
