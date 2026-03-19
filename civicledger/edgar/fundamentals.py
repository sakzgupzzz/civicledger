"""SEC EDGAR XBRL Fundamentals — bulk financial metrics for all US public companies.

Uses the XBRL Frames API to fetch cross-company financial data in ~20 API calls
(vs per-company which would be 5,000+ calls). Computes margins, ratios, and growth.

Source: https://data.sec.gov/api/xbrl/frames/
Public domain. No API key required. Rate limit: 10 req/sec.
"""

import asyncio
import math
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from civicledger.edgar._client import edgar_get, get_ticker_cik_map

# Duration concepts (income/cash flow): use CYyyyyQq format
DURATION_CONCEPTS = [
    ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"),
    ("NetIncomeLoss", "ProfitLoss"),
    ("GrossProfit", None),
    ("OperatingIncomeLoss", None),
    ("EarningsPerShareBasic", "EarningsPerShareDiluted"),
    ("CommonStockDividendsPerShareDeclared", "CommonStockDividendsPerShareCashPaid"),
]

# Instant concepts (balance sheet): use CYyyyyQqI format
INSTANT_CONCEPTS = [
    ("Assets", None),
    ("Liabilities", None),
    ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
    ("AssetsCurrent", None),
    ("LiabilitiesCurrent", None),
    ("CommonStockSharesOutstanding", "EntityCommonStockSharesOutstanding"),
    ("Inventories", "InventoryNet"),
    ("LongTermDebt", "LongTermDebtNoncurrent"),
]


def _recent_quarters() -> Tuple[List[str], List[str]]:
    """Return (recent_quarters, yoy_quarters) frame labels to try."""
    today = date.today()
    q = (today.month - 1) // 3  # 0-based current quarter
    year = today.year
    if q == 0:
        recent = [f"CY{year - 1}Q4", f"CY{year - 1}Q3"]
        yoy = [f"CY{year - 2}Q4", f"CY{year - 2}Q3"]
    else:
        recent = [f"CY{year}Q{q}", f"CY{year}Q{q - 1}"]
        yoy = [f"CY{year - 1}Q{q}", f"CY{year - 1}Q{q - 1}"]
    return recent, yoy


async def _fetch_frame(concept: str, unit: str, frame: str) -> Dict[int, float]:
    """Fetch one XBRL frame. Returns {cik: value}."""
    path = f"/api/xbrl/frames/us-gaap/{concept}/{unit}/{frame}.json"
    data = await edgar_get(path)
    if not data:
        return {}
    result: Dict[int, float] = {}
    for entry in data.get("data", []):
        cik = entry.get("cik")
        val = entry.get("val")
        if cik is not None and val is not None:
            result[int(cik)] = float(val)
    return result


async def _fetch_concepts(
    concepts: List[Tuple[str, Optional[str]]],
    frame_label: str,
    instant: bool,
) -> Dict[str, Dict[int, float]]:
    """Fetch multiple XBRL concepts for a given frame label."""
    result: Dict[str, Dict[int, float]] = {}
    suffix = "I" if instant else ""
    for primary, alt in concepts:
        data = await _fetch_frame(primary, "USD", f"{frame_label}{suffix}")
        if len(data) < 500 and alt:
            alt_data = await _fetch_frame(alt, "USD", f"{frame_label}{suffix}")
            for cik, val in alt_data.items():
                data.setdefault(cik, val)
        result[primary] = data
    return result


async def fetch_fundamentals() -> Dict[str, Dict[str, Any]]:
    """Fetch fundamentals for all US public companies from EDGAR XBRL frames.

    Returns {ticker: {metric: value, ...}} for all companies with data.
    Metrics include: revenue, net_income, profit_margin, gross_margin,
    operating_margin, return_on_equity, return_on_assets, debt_to_equity,
    current_ratio, quick_ratio, revenue_growth, earnings_growth, eps,
    shares_outstanding, dividends_per_share.
    """
    logger.info("Fetching EDGAR XBRL fundamentals...")

    # Get ticker-CIK mapping
    cik_map = await get_ticker_cik_map()
    if not cik_map:
        logger.error("Could not fetch ticker-CIK map")
        return {}

    # Reverse map: CIK → list of tickers
    cik_to_tickers: Dict[int, List[str]] = {}
    for ticker, cik in cik_map.items():
        cik_to_tickers.setdefault(cik, []).append(ticker)

    recent_qs, yoy_qs = _recent_quarters()
    logger.info(f"EDGAR frames: recent={recent_qs}, yoy={yoy_qs}")

    # Fetch recent quarter data
    recent_data: Dict[str, Dict[int, float]] = {}
    for qi, q_label in enumerate(recent_qs):
        dur = await _fetch_concepts(DURATION_CONCEPTS, q_label, False)
        inst = await _fetch_concepts(INSTANT_CONCEPTS, q_label, True)
        rev_count = len(dur.get("Revenues", {}))
        if rev_count >= 500 or qi == len(recent_qs) - 1:
            recent_data = {**dur, **inst}
            logger.info(f"EDGAR: using {q_label} — {rev_count} companies with revenue data")
            break
        logger.info(f"EDGAR: {q_label} has only {rev_count} revenue entries, trying older quarter")

    # Fetch YoY comparison quarter
    yoy_data: Dict[str, Dict[int, float]] = {}
    for qi, q_label in enumerate(yoy_qs):
        dur = await _fetch_concepts(DURATION_CONCEPTS, q_label, False)
        rev_count = len(dur.get("Revenues", {}))
        if rev_count >= 500 or qi == len(yoy_qs) - 1:
            yoy_data = dur
            logger.info(f"EDGAR: YoY comparison using {q_label} — {rev_count} companies")
            break

    if not recent_data:
        logger.error("EDGAR: no recent data available")
        return {}

    # Compute ratios per CIK, then map to tickers
    results: Dict[str, Dict[str, Any]] = {}
    all_ciks = set()
    for concept_data in recent_data.values():
        all_ciks.update(concept_data.keys())

    for cik in all_ciks:
        tickers = cik_to_tickers.get(cik)
        if not tickers:
            continue

        rev = recent_data.get("Revenues", {}).get(cik)
        ni = recent_data.get("NetIncomeLoss", {}).get(cik)
        gp = recent_data.get("GrossProfit", {}).get(cik)
        oi = recent_data.get("OperatingIncomeLoss", {}).get(cik)
        eps = recent_data.get("EarningsPerShareBasic", {}).get(cik)
        dps = recent_data.get("CommonStockDividendsPerShareDeclared", {}).get(cik)
        assets = recent_data.get("Assets", {}).get(cik)
        liab = recent_data.get("Liabilities", {}).get(cik)
        equity = recent_data.get("StockholdersEquity", {}).get(cik)
        ca = recent_data.get("AssetsCurrent", {}).get(cik)
        cl = recent_data.get("LiabilitiesCurrent", {}).get(cik)
        shares = recent_data.get("CommonStockSharesOutstanding", {}).get(cik)
        inventory = recent_data.get("Inventories", {}).get(cik)

        record: Dict[str, Any] = {"cik": cik}

        # Raw values
        if rev is not None:
            record["revenue"] = rev
        if ni is not None:
            record["net_income"] = ni
        if gp is not None:
            record["gross_profit"] = gp
        if oi is not None:
            record["operating_income"] = oi
        if eps is not None:
            record["eps"] = eps
        if dps is not None:
            record["dividends_per_share"] = dps
        if assets is not None:
            record["total_assets"] = assets
        if liab is not None:
            record["total_liabilities"] = liab
        if equity is not None:
            record["stockholders_equity"] = equity
        if ca is not None:
            record["current_assets"] = ca
        if cl is not None:
            record["current_liabilities"] = cl
        if shares is not None:
            record["shares_outstanding"] = shares
        if inventory is not None:
            record["inventory"] = inventory

        # Margin ratios
        if rev and rev > 0:
            if ni is not None:
                record["profit_margin"] = round(ni / rev, 4)
            if gp is not None:
                record["gross_margin"] = round(gp / rev, 4)
            if oi is not None:
                record["operating_margin"] = round(oi / rev, 4)
            if shares and shares > 0:
                record["revenue_per_share"] = round(rev / shares, 2)

        # Return ratios
        if ni is not None:
            if equity and equity > 0:
                record["return_on_equity"] = round(ni / equity, 4)
            if assets and assets > 0:
                record["return_on_assets"] = round(ni / assets, 4)

        # Leverage ratios
        if equity and equity > 0 and liab is not None:
            record["debt_to_equity"] = round(liab / equity, 2)
        if cl and cl > 0 and ca is not None:
            record["current_ratio"] = round(ca / cl, 2)
            inv = inventory if inventory is not None else 0
            record["quick_ratio"] = round((ca - inv) / cl, 2)

        # Growth (YoY quarterly)
        rev_yoy = yoy_data.get("Revenues", {}).get(cik)
        ni_yoy = yoy_data.get("NetIncomeLoss", {}).get(cik)
        if rev and rev_yoy and rev_yoy > 0:
            record["revenue_growth"] = round((rev / rev_yoy) - 1, 4)
        if ni and ni_yoy and ni_yoy > 0:
            record["earnings_growth"] = round((ni / ni_yoy) - 1, 4)

        if len(record) > 1:  # more than just cik
            for ticker in tickers:
                results[ticker] = dict(record)

    logger.info(f"EDGAR fundamentals: computed metrics for {len(results)} tickers")
    return results
