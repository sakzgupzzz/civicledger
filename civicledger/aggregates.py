"""Cross-cutting aggregations — trending tickers and leaderboards.

Rolls up the raw disclosure feeds into "who's trading what" views:
  - congress_leaderboard(): most-traded tickers by US House members.
  - insider_leaderboard(): tickers with the most insider buying / selling.

Both are derived from the existing fetchers and cached on disk, since they
parse many filings.
"""

from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from loguru import logger

from civicledger.cache import cached

_TTL = 3600  # 1h


async def congress_leaderboard(year: Optional[int] = None, limit: int = 25) -> List[Dict[str, Any]]:
    """Most-traded tickers by US House members for a year.

    Returns list of {ticker, trades, buys, sells, members, last_date},
    sorted by trade count.
    """
    yr = year or date.today().year

    async def _build():
        from civicledger.congress.trades import fetch_house_trades_detailed
        trades = await fetch_house_trades_detailed(year=yr, limit=600, max_pdf_parse=90)
        agg: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {"trades": 0, "buys": 0, "sells": 0, "members": set(), "last_date": ""}
        )
        for t in trades:
            tk = t.get("ticker")
            if not tk:
                continue
            r = agg[tk]
            r["trades"] += 1
            typ = (t.get("transaction_type") or "").lower()
            if "purchase" in typ:
                r["buys"] += 1
            elif "sale" in typ:
                r["sells"] += 1
            if t.get("politician"):
                r["members"].add(t["politician"])
            d = t.get("transaction_date") or ""
            if d > r["last_date"]:
                r["last_date"] = d
        rows = [
            {"ticker": tk, "trades": r["trades"], "buys": r["buys"], "sells": r["sells"],
             "members": len(r["members"]), "last_date": r["last_date"]}
            for tk, r in agg.items()
        ]
        rows.sort(key=lambda x: (x["trades"], x["members"]), reverse=True)
        return rows

    rows = await cached(f"agg/congress/{yr}", _TTL, _build)
    logger.info(f"congress leaderboard {yr}: {len(rows)} tickers")
    return rows[:limit]


async def insider_leaderboard(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    limit: int = 25,
) -> List[Dict[str, Any]]:
    """Tickers with the most insider activity over a window.

    Returns list of {ticker, company, buy_value, sell_value, net_value,
    buys, sells, insiders}, sorted by gross dollar activity.
    """
    td = to_date or date.today().isoformat()
    fd = from_date or (date.today() - timedelta(days=7)).isoformat()

    async def _build():
        from civicledger.edgar.insider_trades import fetch_recent_insider_trades
        trades = await fetch_recent_insider_trades(fd, td, limit=400)
        agg: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {"company": None, "buy_value": 0.0, "sell_value": 0.0,
                     "buys": 0, "sells": 0, "insiders": set()}
        )
        for t in trades:
            tk = t.get("ticker")
            if not tk:
                continue
            r = agg[tk]
            r["company"] = r["company"] or t.get("company")
            val = t.get("total_value") or 0
            typ = (t.get("transaction_type") or "").lower()
            if "purchase" in typ or "buy" in typ:
                r["buy_value"] += val
                r["buys"] += 1
            elif "sale" in typ or "sell" in typ:
                r["sell_value"] += val
                r["sells"] += 1
            if t.get("insider_name"):
                r["insiders"].add(t["insider_name"])
        rows = []
        for tk, r in agg.items():
            rows.append({
                "ticker": tk, "company": r["company"],
                "buy_value": round(r["buy_value"]), "sell_value": round(r["sell_value"]),
                "net_value": round(r["buy_value"] - r["sell_value"]),
                "buys": r["buys"], "sells": r["sells"], "insiders": len(r["insiders"]),
            })
        rows.sort(key=lambda x: x["buy_value"] + x["sell_value"], reverse=True)
        return rows

    rows = await cached(f"agg/insider/{fd}/{td}", _TTL, _build)
    logger.info(f"insider leaderboard {fd}..{td}: {len(rows)} tickers")
    return rows[:limit]
