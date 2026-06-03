"""SEC EDGAR XBRL Fundamentals — bulk financial metrics for all US public companies.

Uses the XBRL Frames API to fetch cross-company financial data in ~20 API calls
(vs per-company which would be 5,000+ calls). Computes margins, ratios, and growth.

Source: https://data.sec.gov/api/xbrl/frames/
Public domain. No API key required. Rate limit: 10 req/sec.
"""

import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from civicledger.edgar._client import edgar_get, get_ticker_cik_map

# Concept keys carried through the computation, in record order.
_RECORD_KEYS = [
    "Revenues", "NetIncomeLoss", "GrossProfit", "OperatingIncomeLoss",
    "EarningsPerShareBasic", "CommonStockDividendsPerShareDeclared",
    "Assets", "Liabilities", "StockholdersEquity", "AssetsCurrent",
    "LiabilitiesCurrent", "CommonStockSharesOutstanding", "Inventories",
]


def _build_record(v: Dict[str, Optional[float]], yoy: Dict[str, Optional[float]]) -> Dict[str, Any]:
    """Compute a fundamentals record (raw values + ratios + growth) from a
    dict of concept -> latest value and a YoY dict (Revenues, NetIncomeLoss).
    Shared by the bulk-frames path and the single-ticker companyfacts path.
    """
    rev = v.get("Revenues")
    ni = v.get("NetIncomeLoss")
    gp = v.get("GrossProfit")
    oi = v.get("OperatingIncomeLoss")
    eps = v.get("EarningsPerShareBasic")
    dps = v.get("CommonStockDividendsPerShareDeclared")
    assets = v.get("Assets")
    liab = v.get("Liabilities")
    equity = v.get("StockholdersEquity")
    ca = v.get("AssetsCurrent")
    cl = v.get("LiabilitiesCurrent")
    shares = v.get("CommonStockSharesOutstanding")
    inventory = v.get("Inventories")

    record: Dict[str, Any] = {}
    for key, val in [
        ("revenue", rev), ("net_income", ni), ("gross_profit", gp),
        ("operating_income", oi), ("eps", eps), ("dividends_per_share", dps),
        ("total_assets", assets), ("total_liabilities", liab),
        ("stockholders_equity", equity), ("current_assets", ca),
        ("current_liabilities", cl), ("shares_outstanding", shares),
        ("inventory", inventory),
    ]:
        if val is not None:
            record[key] = val

    if rev and rev > 0:
        if ni is not None:
            record["profit_margin"] = round(ni / rev, 4)
        if gp is not None:
            record["gross_margin"] = round(gp / rev, 4)
        if oi is not None:
            record["operating_margin"] = round(oi / rev, 4)
        if shares and shares > 0:
            record["revenue_per_share"] = round(rev / shares, 2)

    if ni is not None:
        if equity and equity > 0:
            record["return_on_equity"] = round(ni / equity, 4)
        if assets and assets > 0:
            record["return_on_assets"] = round(ni / assets, 4)

    if equity and equity > 0 and liab is not None:
        record["debt_to_equity"] = round(liab / equity, 2)
    if cl and cl > 0 and ca is not None:
        record["current_ratio"] = round(ca / cl, 2)
        inv = inventory if inventory is not None else 0
        record["quick_ratio"] = round((ca - inv) / cl, 2)

    rev_yoy = yoy.get("Revenues")
    ni_yoy = yoy.get("NetIncomeLoss")
    if rev and rev_yoy and rev_yoy > 0:
        record["revenue_growth"] = round((rev / rev_yoy) - 1, 4)
    if ni and ni_yoy and ni_yoy > 0:
        record["earnings_growth"] = round((ni / ni_yoy) - 1, 4)

    return record

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

        vals = {k: recent_data.get(k, {}).get(cik) for k in _RECORD_KEYS}
        yoy = {
            "Revenues": yoy_data.get("Revenues", {}).get(cik),
            "NetIncomeLoss": yoy_data.get("NetIncomeLoss", {}).get(cik),
        }
        record = _build_record(vals, yoy)
        if record:  # has at least one metric
            record["cik"] = cik
            for ticker in tickers:
                results[ticker] = dict(record)

    logger.info(f"EDGAR fundamentals: computed metrics for {len(results)} tickers")
    return results


# ---------------------------------------------------------------------------
# Single-ticker fast path — one companyfacts call instead of ~20 frame calls
# ---------------------------------------------------------------------------

# (concept key, [us-gaap/dei tags], unit, instant)
_TICKER_SPECS: List[Tuple[str, List[str], str, bool]] = [
    ("Revenues", ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"], "USD", False),
    ("NetIncomeLoss", ["NetIncomeLoss", "ProfitLoss"], "USD", False),
    ("GrossProfit", ["GrossProfit"], "USD", False),
    ("OperatingIncomeLoss", ["OperatingIncomeLoss"], "USD", False),
    ("EarningsPerShareBasic", ["EarningsPerShareBasic", "EarningsPerShareDiluted"], "USD/shares", False),
    ("CommonStockDividendsPerShareDeclared",
     ["CommonStockDividendsPerShareDeclared", "CommonStockDividendsPerShareCashPaid"], "USD/shares", False),
    ("Assets", ["Assets"], "USD", True),
    ("Liabilities", ["Liabilities"], "USD", True),
    ("StockholdersEquity",
     ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"], "USD", True),
    ("AssetsCurrent", ["AssetsCurrent"], "USD", True),
    ("LiabilitiesCurrent", ["LiabilitiesCurrent"], "USD", True),
    ("CommonStockSharesOutstanding",
     ["CommonStockSharesOutstanding", "EntityCommonStockSharesOutstanding"], "shares", True),
    ("Inventories", ["Inventories", "InventoryNet"], "USD", True),
]

_FRAME_Q = re.compile(r"^CY(\d{4})Q([1-4])$")
_FRAME_QI = re.compile(r"^CY(\d{4})Q([1-4])I$")


def _pick_quarter(entries: List[dict], instant: bool, target: Optional[str] = None) -> Optional[float]:
    """From companyfacts unit entries, pick the value for ``target`` frame, else
    the most recent quarterly frame. Only frame-tagged (cross-comparable)
    quarterly values are considered, mirroring the bulk frames path.
    """
    rx = _FRAME_QI if instant else _FRAME_Q
    by_frame: Dict[str, float] = {}
    best: Optional[Tuple[int, int, float]] = None
    for e in entries:
        frame = e.get("frame")
        val = e.get("val")
        if not frame or val is None:
            continue
        m = rx.match(frame)
        if not m:
            continue
        by_frame[frame] = val
        y, q = int(m.group(1)), int(m.group(2))
        if best is None or (y, q) > (best[0], best[1]):
            best = (y, q, val)
    if target and target in by_frame:
        return by_frame[target]
    return best[2] if best else None


async def fetch_fundamentals_for_ticker(ticker: str) -> Optional[Dict[str, Any]]:
    """Fetch fundamentals for ONE ticker via the EDGAR companyfacts API.

    A single HTTP call (plus the cached ticker->CIK map) instead of the ~20
    cross-company frame calls in fetch_fundamentals(). Returns the same metric
    schema, plus ``company`` and ``period``, or None if the ticker has no data.
    """
    tk = ticker.upper()
    cik_map = await get_ticker_cik_map()
    cik = cik_map.get(tk)
    if not cik:
        logger.debug(f"fundamentals: no CIK for {tk}")
        return None

    facts = await edgar_get(f"/api/xbrl/companyfacts/CIK{cik:010d}.json")
    if not facts:
        return None

    namespaces = facts.get("facts", {})

    def entries_for(tags: List[str], unit: str) -> List[dict]:
        out: List[dict] = []
        for ns in ("us-gaap", "dei"):
            block = namespaces.get(ns, {})
            for tag in tags:
                units = block.get(tag, {}).get("units", {})
                out.extend(units.get(unit, []))
        return out

    # Anchor on revenue's most recent quarterly frame so other concepts align.
    rev_entries = entries_for(_TICKER_SPECS[0][1], "USD")
    rev_frame: Optional[str] = None
    best: Optional[Tuple[int, int]] = None
    for e in rev_entries:
        m = _FRAME_Q.match(e.get("frame") or "")
        if m and e.get("val") is not None:
            yq = (int(m.group(1)), int(m.group(2)))
            if best is None or yq > best:
                best, rev_frame = yq, e["frame"]

    vals: Dict[str, Optional[float]] = {}
    for key, tags, unit, instant in _TICKER_SPECS:
        target = (rev_frame + "I" if instant else rev_frame) if rev_frame else None
        vals[key] = _pick_quarter(entries_for(tags, unit), instant, target=target)

    yoy: Dict[str, Optional[float]] = {}
    if best:
        yoy_frame = f"CY{best[0] - 1}Q{best[1]}"
        yoy["Revenues"] = _pick_quarter(rev_entries, False, target=yoy_frame)
        yoy["NetIncomeLoss"] = _pick_quarter(
            entries_for(_TICKER_SPECS[1][1], "USD"), False, target=yoy_frame
        )

    record = _build_record(vals, yoy)
    if not record:
        return None
    record["cik"] = cik
    record["company"] = facts.get("entityName")
    if rev_frame:
        record["period"] = rev_frame
    logger.info(f"EDGAR fundamentals (single): {tk} — {len(record)} fields")
    return record
