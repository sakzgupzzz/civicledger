"""SEC EDGAR 13F Institutional Holdings — what hedge funds and institutions own.

13F filings are required quarterly from institutional investment managers with
$100M+ in AUM. Covers equity positions (stocks, ETFs, convertible bonds).

Uses edgartools for parsing 13F-HR filings.

Source: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=13F
Public domain. No API key required.
"""

from typing import Any, Dict, List, Optional

from loguru import logger


def _to_number(v: Any) -> Optional[float]:
    """Coerce 13F values/shares (which may be strings with commas) to a number."""
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


# Well-known institutional investors for prefetching
TOP_INSTITUTIONS = [
    ("Berkshire Hathaway", "0001067983"),
    ("Bridgewater Associates", "0001350694"),
    ("Renaissance Technologies", "0001037389"),
    ("Citadel Advisors", "0001423053"),
    ("BlackRock", "0001364742"),
    ("Vanguard Group", "0000102909"),
    ("State Street", "0000093751"),
    ("ARK Invest", "0001697748"),
    ("Soros Fund Management", "0001029160"),
    ("Appaloosa Management", "0001656456"),
    ("Pershing Square", "0001336528"),
    ("Two Sigma", "0001179392"),
    ("DE Shaw", "0001009207"),
    ("Point72", "0001603466"),
    ("Tiger Global", "0001167483"),
]


async def fetch_holdings(
    manager_name_or_cik: str,
    limit: int = 100,
) -> Dict[str, Any]:
    """Fetch latest 13F holdings for an institutional manager.

    Args:
        manager_name_or_cik: Manager name (e.g., "Berkshire Hathaway")
                             or CIK number (e.g., "0001067983")
        limit: Max number of holdings to return

    Returns dict with: manager_name, manager_cik, period, filing_date,
    total_value, holdings: [{ticker, company, cusip, shares, value_thousands,
    share_change, change_percent}]
    """
    try:
        from edgar import set_identity, Company
        from civicledger.config import get_settings

        import os
        os.environ.setdefault("EDGAR_LOCAL_CACHE", "/tmp/edgar_cache")

        settings = get_settings()
        set_identity(settings.edgar_identity)

        # Look up by CIK or name
        if manager_name_or_cik.isdigit() or manager_name_or_cik.startswith("0"):
            cik = int(manager_name_or_cik.lstrip("0"))
            company = Company(cik)
        else:
            company = Company(manager_name_or_cik)

        # Get latest 13F-HR filing
        filings_13f = company.get_filings(form="13F-HR")
        if not filings_13f or len(filings_13f) == 0:
            return {"error": f"No 13F filings found for {manager_name_or_cik}"}

        latest = filings_13f[0]
        obj = latest.obj()

        if not obj:
            return {"error": "Could not parse 13F filing"}

        # Extract holdings from the 13F information table. edgartools resolves
        # CUSIP -> Ticker for us, so prefer the infotable DataFrame.
        holdings: List[Dict[str, Any]] = []
        total_value = 0

        df = getattr(obj, "infotable", None)
        if df is not None and not df.empty:
            # Column names from edgartools (capitalized): Issuer, Cusip, Value,
            # SharesPrnAmount, Ticker, PutCall, Class.
            cols = set(df.columns)

            def _col(row, *names):
                for n in names:
                    if n in cols:
                        v = row.get(n)
                        if v is not None and str(v) != "" and str(v).lower() != "nan":
                            return v
                return None

            for _, row in df.iterrows():
                value = _to_number(_col(row, "Value", "value"))
                shares = _to_number(_col(row, "SharesPrnAmount", "shares"))
                ticker = _col(row, "Ticker", "ticker")
                if value:
                    total_value += value

                holdings.append({
                    "ticker": str(ticker).strip().upper() if ticker else None,
                    "company": str(_col(row, "Issuer", "nameOfIssuer", "name") or "").strip() or None,
                    "cusip": str(_col(row, "Cusip", "cusip") or "").strip() or None,
                    "class": str(_col(row, "Class", "class") or "").strip() or None,
                    "put_call": _col(row, "PutCall", "putCall") or None,
                    "shares": int(shares) if shares else None,
                    "value_usd": int(value) if value else None,
                })

        # Aggregate multiple rows for the same issuer (managers often list a
        # position across several internal accounts).
        merged: Dict[str, Dict[str, Any]] = {}
        for h in holdings:
            key = h.get("cusip") or h.get("ticker") or h.get("company") or id(h)
            key = f"{key}|{h.get('put_call') or ''}"
            if key in merged:
                m = merged[key]
                m["shares"] = (m.get("shares") or 0) + (h.get("shares") or 0)
                m["value_usd"] = (m.get("value_usd") or 0) + (h.get("value_usd") or 0)
            else:
                merged[key] = dict(h)
        holdings = list(merged.values())
        holdings.sort(key=lambda h: h.get("value_usd") or 0, reverse=True)

        result = {
            "manager_name": str(company),
            "manager_cik": getattr(company, "cik", None),
            "period": str(getattr(latest, "period_of_report", latest.filing_date)),
            "filing_date": str(latest.filing_date),
            "total_value_millions": round(total_value / 1_000_000, 1) if total_value else None,
            "holdings_count": len(holdings),
            "holdings": holdings[:limit],
        }

        logger.info(
            f"13F holdings for {manager_name_or_cik}: "
            f"{len(holdings)} positions, ${result.get('total_value_millions', 0)}M total"
        )
        return result

    except ImportError:
        logger.warning("edgartools not installed — 13F holdings unavailable")
        return {"error": "edgartools not installed"}
    except Exception as e:
        logger.warning(f"13F fetch failed for {manager_name_or_cik}: {e}")
        return {"error": str(e)}


async def fetch_top_institutions_summary() -> List[Dict[str, Any]]:
    """Fetch summary for top institutional investors.

    Returns list of {manager_name, manager_cik, total_value_millions, holdings_count}.
    """
    results = []
    for name, cik in TOP_INSTITUTIONS:
        try:
            data = await fetch_holdings(cik, limit=5)
            if "error" not in data:
                results.append({
                    "manager_name": name,
                    "manager_cik": cik,
                    "total_value_millions": data.get("total_value_millions"),
                    "holdings_count": data.get("holdings_count"),
                    "period": data.get("period"),
                    "top_holdings": data.get("holdings", [])[:5],
                })
        except Exception as e:
            logger.debug(f"Failed to fetch 13F for {name}: {e}")
    return results
