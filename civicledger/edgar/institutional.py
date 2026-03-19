"""SEC EDGAR 13F Institutional Holdings — what hedge funds and institutions own.

13F filings are required quarterly from institutional investment managers with
$100M+ in AUM. Covers equity positions (stocks, ETFs, convertible bonds).

Uses edgartools for parsing 13F-HR filings.

Source: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=13F
Public domain. No API key required.
"""

from typing import Any, Dict, List, Optional

from loguru import logger


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

        # Extract holdings
        holdings: List[Dict[str, Any]] = []
        total_value = 0

        if hasattr(obj, "infotable") and obj.infotable is not None:
            df = obj.infotable
            for _, row in df.iterrows():
                cusip = str(row.get("cusip", "")) if "cusip" in df.columns else None
                shares = row.get("shrsOrPrnAmt_sshPrnamt") or row.get("shares")
                value = row.get("value")
                name = row.get("nameOfIssuer") or row.get("name", "")

                if value:
                    total_value += int(value)

                holdings.append({
                    "company": str(name).strip(),
                    "cusip": cusip,
                    "shares": int(shares) if shares else None,
                    "value_thousands": int(value) if value else None,
                    "ticker": None,  # CUSIP → ticker mapping needed
                })
        elif hasattr(obj, "holdings"):
            for h in obj.holdings:
                holdings.append({
                    "company": getattr(h, "name", None),
                    "cusip": getattr(h, "cusip", None),
                    "shares": getattr(h, "shares", None),
                    "value_thousands": getattr(h, "value", None),
                    "ticker": None,
                })

        # Sort by value descending
        holdings.sort(key=lambda h: h.get("value_thousands") or 0, reverse=True)

        result = {
            "manager_name": str(company),
            "manager_cik": getattr(company, "cik", None),
            "period": str(getattr(latest, "period_of_report", latest.filing_date)),
            "filing_date": str(latest.filing_date),
            "total_value_millions": round(total_value / 1000, 1) if total_value else None,
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
