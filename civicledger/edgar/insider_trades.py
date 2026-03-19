"""SEC EDGAR Insider Trades — from Form 4 filings.

Form 4 = "Statement of Changes in Beneficial Ownership" — filed within
2 business days when officers, directors, or 10%+ owners buy/sell stock.

Uses edgartools for parsing individual Form 4 filings, and EFTS for
finding recent filings across all companies.

Source: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=4
Public domain. No API key required.
"""

import asyncio
import re
from typing import Any, Dict, List, Optional

from loguru import logger

from civicledger.edgar._client import efts_search, edgar_get


async def fetch_recent_insider_trades(
    from_date: str,
    to_date: str,
    ticker: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fetch recent insider trades from SEC EDGAR.

    Uses the EDGAR EFTS API to find Form 4 filings, then parses
    each filing via the SEC submissions API for structured data.

    Args:
        from_date: Start date (YYYY-MM-DD)
        to_date: End date (YYYY-MM-DD)
        ticker: Optional — filter to a specific ticker

    Returns list of {ticker, company, insider_name, insider_title,
    transaction_type, transaction_date, shares, price_per_share,
    total_value, shares_owned_after, filing_date}.
    """
    query = '"BENEFICIAL OWNERSHIP"'  # Present in every Form 4
    if ticker:
        query = f'"{ticker}"'

    all_trades: List[Dict[str, Any]] = []
    page = 0

    while True:
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
            file_num = s.get("file_num", "")

            # Parse display names for company + insider info
            # Form 4 display_names typically has: [reporting_person, issuer_company]
            if len(display_names) < 2:
                continue

            # Try to extract ticker from the issuer name
            ticker_re = re.compile(r"\(([A-Z]{1,5})\)")
            issuer_name = display_names[-1] if display_names else ""
            insider_name_raw = display_names[0] if display_names else ""

            ticker_match = ticker_re.search(issuer_name)
            found_ticker = ticker_match.group(1) if ticker_match else None

            if ticker and found_ticker and found_ticker != ticker.upper():
                continue

            company = issuer_name.split("(")[0].strip() if issuer_name else None
            insider_name = insider_name_raw.split("(")[0].strip() if insider_name_raw else None

            if not found_ticker or not insider_name:
                continue

            all_trades.append({
                "ticker": found_ticker,
                "company": company,
                "insider_name": insider_name,
                "insider_title": None,  # Would need to parse the actual filing
                "transaction_type": None,  # Would need to parse the actual filing
                "transaction_date": filing_date,
                "shares": None,
                "price_per_share": None,
                "total_value": None,
                "shares_owned_after": None,
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
        key = (t["ticker"], t["insider_name"], t["filing_date"])
        if key not in seen:
            seen.add(key)
            deduped.append(t)

    deduped.sort(key=lambda x: x.get("filing_date", ""), reverse=True)
    logger.info(f"EDGAR insider trades: {len(deduped)} filings for {from_date} to {to_date}")
    return deduped


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
                obj = filing.obj()
                if not obj:
                    continue

                # edgartools Form4 object has ownership data
                if hasattr(obj, "transactions"):
                    for txn in obj.transactions:
                        trades.append({
                            "ticker": ticker.upper(),
                            "company": getattr(obj, "issuer_name", None),
                            "insider_name": getattr(obj, "reporting_owner_name", None),
                            "insider_title": getattr(obj, "reporting_owner_title", None),
                            "transaction_type": getattr(txn, "transaction_code_description", None),
                            "transaction_date": str(getattr(txn, "transaction_date", "")),
                            "shares": getattr(txn, "shares", None),
                            "price_per_share": getattr(txn, "price_per_share", None),
                            "total_value": None,
                            "shares_owned_after": getattr(txn, "shares_owned_after", None),
                            "filing_date": str(filing.filing_date),
                        })
                else:
                    # Fallback: basic info from filing metadata
                    trades.append({
                        "ticker": ticker.upper(),
                        "company": None,
                        "insider_name": getattr(filing, "company", str(filing)),
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
