"""SEC EDGAR Insider Trades — from Form 4 filings.

Form 4 = "Statement of Changes in Beneficial Ownership" — filed within
2 business days when officers, directors, or 10%+ owners buy/sell stock.

Uses edgartools to parse Form 4 filings into real transaction detail
(insider name, title, shares, price, value, transaction type) rather than
filing metadata alone.

Source: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=4
Public domain. No API key required.
"""

import asyncio
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from loguru import logger

from civicledger.edgar._client import ensure_edgar_identity

# Concurrency cap when parsing many Form 4 XMLs from live EDGAR.
_PARSE_CONCURRENCY = 8


def _to_float(v: Any) -> Optional[float]:
    """Coerce to float, tolerating footnote markers (e.g. '[F1]') and blanks."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> Optional[int]:
    """Coerce to int, tolerating footnote markers and blanks."""
    f = _to_float(v)
    return int(f) if f is not None else None


def _edgar_ready() -> bool:
    """Configure edgartools identity + cache. Returns False if not installed."""
    return ensure_edgar_identity()


def _summary_to_transactions(summary: Any, ticker: Optional[str] = None) -> List[Dict[str, Any]]:
    """Expand a Form 4 ownership summary into one row per transaction.

    ``summary`` is edgartools' TransactionSummary (from Form4.get_ownership_summary()).
    Each contained TransactionActivity becomes a fully-populated trade row.
    """
    rows: List[Dict[str, Any]] = []
    issuer_ticker = getattr(summary, "issuer_ticker", None) or ticker
    base = {
        "ticker": issuer_ticker.upper() if issuer_ticker else None,
        "company": getattr(summary, "issuer_name", None),
        "insider_name": getattr(summary, "insider_name", None),
        "insider_title": getattr(summary, "position", None),
        "filing_date": getattr(summary, "reporting_date", None),
    }
    remaining = _to_int(getattr(summary, "remaining_shares", None))
    for txn in getattr(summary, "transactions", []) or []:
        rows.append({
            **base,
            "transaction_type": getattr(txn, "transaction_type", None),
            "transaction_code": getattr(txn, "code", None),
            "security_title": getattr(txn, "security_title", None),
            "security_type": getattr(txn, "security_type", None),
            "transaction_date": getattr(summary, "reporting_date", None),
            "shares": _to_int(getattr(txn, "shares", None)),
            "price_per_share": _to_float(getattr(txn, "price_per_share", None)),
            "total_value": _to_float(getattr(txn, "value", None)),
            "shares_owned_after": remaining,
        })

    # If a filing had no individual transactions, still emit a summary row so
    # the filing is visible rather than silently dropped.
    if not rows:
        rows.append({
            **base,
            "transaction_type": getattr(summary, "primary_activity", None),
            "transaction_code": None,
            "security_title": None,
            "security_type": None,
            "transaction_date": getattr(summary, "reporting_date", None),
            "shares": None,
            "price_per_share": None,
            "total_value": None,
            "shares_owned_after": None,
        })
    return rows


def _parse_filing(filing: Any, ticker: Optional[str] = None) -> List[Dict[str, Any]]:
    """Parse a single Form 4 Filing into trade rows (blocking; run in a thread)."""
    try:
        obj = filing.obj()
        summary = obj.get_ownership_summary()
        rows = _summary_to_transactions(summary, ticker=ticker)
        for r in rows:
            r.setdefault("cik", getattr(filing, "cik", None))
        return rows
    except Exception as e:  # noqa: BLE001 - one bad filing shouldn't kill the batch
        logger.debug(f"Form 4 parse failed ({getattr(filing, 'accession_no', '?')}): {e}")
        return []


async def fetch_insider_trades_detailed(
    ticker: str,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Fetch detailed insider trade history for a specific ticker.

    Parses actual Form 4 XML for each filing and returns one row per
    transaction with shares, price, value, transaction type, insider name,
    and title.
    """
    if not _edgar_ready():
        logger.warning("edgartools not installed — detailed insider trades unavailable")
        return []

    from edgar import Company

    try:
        company = await asyncio.to_thread(Company, ticker)
        filings = await asyncio.to_thread(lambda: company.get_filings(form="4"))
        if not filings or len(filings) == 0:
            return []

        # Parse newest filings until we have `limit` transaction rows. Each
        # filing can yield several rows, so cap the filings we touch generously.
        to_parse = list(filings)[: max(limit, 25)]
        sem = asyncio.Semaphore(_PARSE_CONCURRENCY)

        async def _one(f):
            async with sem:
                return await asyncio.to_thread(_parse_filing, f, ticker.upper())

        batches = await asyncio.gather(*[_one(f) for f in to_parse])
        trades: List[Dict[str, Any]] = [row for batch in batches for row in batch]
        trades.sort(key=lambda x: x.get("transaction_date") or "", reverse=True)
        logger.info(f"EDGAR insider trades (detailed): {len(trades)} rows for {ticker}")
        return trades[:limit]

    except Exception as e:  # noqa: BLE001
        logger.warning(f"EDGAR detailed insider trades failed for {ticker}: {e}")
        return []


async def fetch_recent_insider_trades(
    from_date: str,
    to_date: str,
    ticker: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Fetch recent Form 4 insider trades across the whole market.

    Iterates each day in [from_date, to_date] (fixing the prior single-day
    bug) and pulls Form 4 filings. Returns one parsed row per transaction
    with real insider name, ticker, shares, price, and value.

    Args:
        from_date: Start date (YYYY-MM-DD)
        to_date: End date (YYYY-MM-DD)
        ticker: Optional — filter to a specific issuer ticker
        limit: Max transaction rows to return

    Returns list of trade dicts (see _summary_to_transactions for fields).
    """
    if not _edgar_ready():
        logger.warning("edgartools not installed — using EFTS fallback for insider trades")
        return await _fetch_via_efts(from_date, to_date, ticker, limit)

    from edgar import get_filings

    want = ticker.upper() if ticker else None
    start = date.fromisoformat(from_date)
    end = date.fromisoformat(to_date)

    # Collect Form 4 filings day by day; isolate per-day index failures.
    day_filings: List[Any] = []
    d = end  # newest first
    while d >= start and len(day_filings) < limit * 3:
        try:
            fs = await asyncio.to_thread(
                lambda day=d: get_filings(form="4", filing_date=day.isoformat())
            )
            if fs:
                day_filings.extend(list(fs))
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Form 4 index unavailable for {d}: {e}")
        d -= timedelta(days=1)

    if not day_filings:
        return await _fetch_via_efts(from_date, to_date, ticker, limit)

    sem = asyncio.Semaphore(_PARSE_CONCURRENCY)

    async def _one(f):
        async with sem:
            return await asyncio.to_thread(_parse_filing, f, None)

    batches = await asyncio.gather(*[_one(f) for f in day_filings[: limit * 3]])
    trades: List[Dict[str, Any]] = [row for batch in batches for row in batch]

    if want:
        trades = [t for t in trades if t.get("ticker") == want]

    # Dedupe by (insider, ticker, date, shares, code)
    seen: set = set()
    deduped: List[Dict[str, Any]] = []
    for t in trades:
        key = (
            t.get("insider_name"),
            t.get("ticker"),
            t.get("transaction_date"),
            t.get("shares"),
            t.get("transaction_code"),
        )
        if key not in seen:
            seen.add(key)
            deduped.append(t)

    deduped.sort(key=lambda x: x.get("transaction_date") or "", reverse=True)
    logger.info(f"EDGAR insider trades: {len(deduped)} rows for {from_date} to {to_date}")
    return deduped[:limit]


async def _fetch_via_efts(
    from_date: str, to_date: str, ticker: Optional[str], limit: int
) -> List[Dict[str, Any]]:
    """Fallback: EFTS full-text search for Form 4 filings (metadata only)."""
    import re

    from civicledger.edgar._client import EFTS_PAGE_SIZE, efts_search

    ticker_re = re.compile(r"\(([A-Z]{1,5})\)")
    query = f'"{ticker}"' if ticker else '"securities"'

    all_trades: List[Dict[str, Any]] = []
    page = 0
    fetched = 0

    while len(all_trades) < limit:
        data = await efts_search(
            query=query, forms="4", start_date=from_date, end_date=to_date,
            page=page, size=EFTS_PAGE_SIZE,
        )
        if not data:
            break

        hits = data.get("hits", {}).get("hits", [])
        total = data.get("hits", {}).get("total", {}).get("value", 0)
        fetched += len(hits)

        for h in hits:
            s = h.get("_source", {})
            display_names = s.get("display_names", [])
            ciks = s.get("ciks", [])
            if len(display_names) < 2:
                continue
            issuer_name = display_names[-1]
            insider_name_raw = display_names[0]
            m = ticker_re.search(issuer_name)
            found_ticker = m.group(1) if m else None
            if ticker and found_ticker and found_ticker != ticker.upper():
                continue
            all_trades.append({
                "ticker": found_ticker,
                "company": issuer_name.split("(")[0].strip(),
                "insider_name": insider_name_raw.split("(")[0].strip(),
                "insider_title": None,
                "transaction_type": None,
                "transaction_date": s.get("file_date"),
                "shares": None,
                "price_per_share": None,
                "total_value": None,
                "shares_owned_after": None,
                "filing_date": s.get("file_date"),
                "cik": int(ciks[-1]) if ciks else None,
            })

        if not hits or fetched >= total:
            break
        page += 1
        await asyncio.sleep(0.12)

    seen: set = set()
    deduped: List[Dict[str, Any]] = []
    for t in all_trades:
        key = (t.get("insider_name"), t.get("company"), t.get("filing_date"))
        if key not in seen:
            seen.add(key)
            deduped.append(t)
    deduped.sort(key=lambda x: x.get("filing_date") or "", reverse=True)
    logger.info(f"EDGAR insider trades (EFTS fallback): {len(deduped)} for {from_date} to {to_date}")
    return deduped[:limit]
