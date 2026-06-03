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


def _edgar_company(manager_name_or_cik: str):
    """Resolve a manager name or CIK to an edgartools Company (blocking)."""
    import os

    from edgar import Company, set_identity
    from civicledger.config import get_settings

    os.environ.setdefault("EDGAR_LOCAL_CACHE", "/tmp/edgar_cache")
    set_identity(get_settings().edgar_identity)
    if manager_name_or_cik.isdigit() or manager_name_or_cik.startswith("0"):
        return Company(int(manager_name_or_cik.lstrip("0")))
    return Company(manager_name_or_cik)


def _parse_holdings(obj: Any) -> List[Dict[str, Any]]:
    """Parse a 13F filing object's information table into merged holdings.

    Returns a list of {ticker, company, cusip, class, put_call, shares,
    value_usd}, aggregated per issuer (managers split positions across
    internal accounts), sorted by value descending.
    """
    df = getattr(obj, "infotable", None)
    if df is None or df.empty:
        return []
    cols = set(df.columns)

    def _col(row, *names):
        for n in names:
            if n in cols:
                v = row.get(n)
                if v is not None and str(v) != "" and str(v).lower() != "nan":
                    return v
        return None

    rows: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        value = _to_number(_col(row, "Value", "value"))
        shares = _to_number(_col(row, "SharesPrnAmount", "shares"))
        ticker = _col(row, "Ticker", "ticker")
        rows.append({
            "ticker": str(ticker).strip().upper() if ticker else None,
            "company": str(_col(row, "Issuer", "nameOfIssuer", "name") or "").strip() or None,
            "cusip": str(_col(row, "Cusip", "cusip") or "").strip() or None,
            "class": str(_col(row, "Class", "class") or "").strip() or None,
            "put_call": _col(row, "PutCall", "putCall") or None,
            "shares": int(shares) if shares else None,
            "value_usd": int(value) if value else None,
        })

    merged: Dict[str, Dict[str, Any]] = {}
    for h in rows:
        key = f"{h.get('cusip') or h.get('ticker') or h.get('company') or id(h)}|{h.get('put_call') or ''}"
        if key in merged:
            m = merged[key]
            m["shares"] = (m.get("shares") or 0) + (h.get("shares") or 0)
            m["value_usd"] = (m.get("value_usd") or 0) + (h.get("value_usd") or 0)
        else:
            merged[key] = dict(h)
    out = list(merged.values())
    out.sort(key=lambda h: h.get("value_usd") or 0, reverse=True)
    return out


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
        company = _edgar_company(manager_name_or_cik)

        filings_13f = company.get_filings(form="13F-HR")
        if not filings_13f or len(filings_13f) == 0:
            return {"error": f"No 13F filings found for {manager_name_or_cik}"}

        latest = filings_13f[0]
        obj = latest.obj()
        if not obj:
            return {"error": "Could not parse 13F filing"}

        holdings = _parse_holdings(obj)
        total_value = sum(h.get("value_usd") or 0 for h in holdings)

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


async def fetch_holdings_changes(
    manager_name_or_cik: str,
    limit: int = 50,
) -> Dict[str, Any]:
    """Compare a manager's two most recent 13F filings (quarter-over-quarter).

    Returns positions the manager opened, exited, added to, or trimmed —
    with share and USD-value deltas — by diffing the two latest 13F-HR
    information tables on CUSIP.
    """
    try:
        company = _edgar_company(manager_name_or_cik)
        filings_13f = company.get_filings(form="13F-HR")
        if not filings_13f or len(filings_13f) < 2:
            return {"error": f"Need two 13F filings to compare for {manager_name_or_cik}"}

        latest, prev = filings_13f[0], filings_13f[1]
        cur = {h["cusip"]: h for h in _parse_holdings(latest.obj()) if h.get("cusip")}
        old = {h["cusip"]: h for h in _parse_holdings(prev.obj()) if h.get("cusip")}

        def _meta(h):
            return {"ticker": h.get("ticker"), "company": h.get("company"), "cusip": h.get("cusip")}

        new_pos, exited, increased, decreased = [], [], [], []
        for cusip, h in cur.items():
            if cusip not in old:
                new_pos.append({**_meta(h), "shares": h.get("shares"), "value_usd": h.get("value_usd")})
            else:
                ds = (h.get("shares") or 0) - (old[cusip].get("shares") or 0)
                if ds == 0:
                    continue
                rec = {
                    **_meta(h),
                    "shares": h.get("shares"),
                    "shares_change": ds,
                    "value_usd": h.get("value_usd"),
                    "value_change_usd": (h.get("value_usd") or 0) - (old[cusip].get("value_usd") or 0),
                }
                (increased if ds > 0 else decreased).append(rec)
        for cusip, h in old.items():
            if cusip not in cur:
                exited.append({**_meta(h), "shares_before": h.get("shares"), "value_before_usd": h.get("value_usd")})

        new_pos.sort(key=lambda x: x.get("value_usd") or 0, reverse=True)
        increased.sort(key=lambda x: x.get("value_change_usd") or 0, reverse=True)
        decreased.sort(key=lambda x: x.get("value_change_usd") or 0)
        exited.sort(key=lambda x: x.get("value_before_usd") or 0, reverse=True)

        return {
            "manager_name": str(company),
            "manager_cik": getattr(company, "cik", None),
            "current_period": str(getattr(latest, "period_of_report", latest.filing_date)),
            "previous_period": str(getattr(prev, "period_of_report", prev.filing_date)),
            "summary": {
                "new": len(new_pos), "exited": len(exited),
                "increased": len(increased), "decreased": len(decreased),
            },
            "new_positions": new_pos[:limit],
            "exited_positions": exited[:limit],
            "increased": increased[:limit],
            "decreased": decreased[:limit],
        }

    except ImportError:
        return {"error": "edgartools not installed"}
    except Exception as e:
        logger.warning(f"13F changes failed for {manager_name_or_cik}: {e}")
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
