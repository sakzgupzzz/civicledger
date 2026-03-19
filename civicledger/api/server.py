"""FastAPI server for CivicLedger.

Run: civicledger serve --port 8080
Or:  uvicorn civicledger.api.server:app --reload

All data from US government public domain sources.
"""

from datetime import date, timedelta
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware


def create_app() -> FastAPI:
    app = FastAPI(
        title="CivicLedger",
        description=(
            "US financial intelligence from public domain sources. "
            "SEC EDGAR, FRED, and congressional disclosures. "
            "No API key required (except FRED)."
        ),
        version="0.1.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": "0.1.0", "sources": ["SEC EDGAR", "FRED", "Senate eFD", "House Clerk"]}

    # ── Fundamentals ──

    @app.get("/fundamentals")
    async def get_fundamentals(ticker: Optional[str] = None):
        """Get XBRL financial metrics for all or a specific company."""
        from civicledger.edgar.fundamentals import fetch_fundamentals
        data = await fetch_fundamentals()
        if ticker:
            result = data.get(ticker.upper())
            if not result:
                return {"error": f"No data for {ticker.upper()}"}
            return {"ticker": ticker.upper(), **result}
        return {"count": len(data), "tickers": list(data.keys())[:50], "sample": {k: data[k] for k in list(data.keys())[:5]}}

    @app.get("/fundamentals/{ticker}")
    async def get_ticker_fundamentals(ticker: str):
        """Get fundamentals for a specific ticker."""
        from civicledger.edgar.fundamentals import fetch_fundamentals
        data = await fetch_fundamentals()
        result = data.get(ticker.upper())
        if not result:
            return {"error": f"No data for {ticker.upper()}"}
        return {"ticker": ticker.upper(), **result}

    # ── Earnings ──

    @app.get("/earnings")
    async def get_earnings(
        from_date: str = Query(default=None),
        to_date: str = Query(default=None),
    ):
        """Get earnings announcements from 8-K Item 2.02 filings."""
        from civicledger.edgar.earnings import fetch_earnings
        today = date.today()
        f = from_date or (today - timedelta(days=7)).isoformat()
        t = to_date or (today + timedelta(days=7)).isoformat()
        data = await fetch_earnings(f, t)
        return {"from_date": f, "to_date": t, "count": len(data), "earnings": data}

    # ── Insider Trades ──

    @app.get("/insider-trades")
    async def get_insider_trades(
        from_date: str = Query(default=None),
        to_date: str = Query(default=None),
        ticker: Optional[str] = Query(default=None),
    ):
        """Get insider trades from Form 4 filings."""
        from civicledger.edgar.insider_trades import fetch_recent_insider_trades
        today = date.today()
        f = from_date or (today - timedelta(days=7)).isoformat()
        t = to_date or today.isoformat()
        data = await fetch_recent_insider_trades(f, t, ticker=ticker)
        return {"from_date": f, "to_date": t, "count": len(data), "trades": data}

    @app.get("/insider-trades/{ticker}")
    async def get_ticker_insider_trades(ticker: str, limit: int = Query(default=50, le=200)):
        """Get detailed insider trades for a specific ticker."""
        from civicledger.edgar.insider_trades import fetch_insider_trades_detailed
        data = await fetch_insider_trades_detailed(ticker, limit=limit)
        return {"ticker": ticker.upper(), "count": len(data), "trades": data}

    # ── Institutional Holdings (13F) ──

    @app.get("/institutions")
    async def get_institutions():
        """Get summary of top institutional investors."""
        from civicledger.edgar.institutional import fetch_top_institutions_summary
        data = await fetch_top_institutions_summary()
        return {"count": len(data), "institutions": data}

    @app.get("/institutions/{manager}")
    async def get_institution_holdings(manager: str, limit: int = Query(default=100, le=500)):
        """Get 13F holdings for a specific institutional manager."""
        from civicledger.edgar.institutional import fetch_holdings
        data = await fetch_holdings(manager, limit=limit)
        return data

    # ── Congressional Trades ──

    @app.get("/congress")
    async def get_congressional_trades(year: Optional[int] = None, limit: int = Query(default=200, le=500)):
        """Get congressional stock trades from Senate eFD and House clerk."""
        from civicledger.congress.trades import fetch_all_congressional_trades
        data = await fetch_all_congressional_trades(year=year, limit=limit)
        return {"year": year or date.today().year, "count": len(data), "trades": data}

    @app.get("/congress/senate")
    async def get_senate_trades(year: Optional[int] = None, limit: int = Query(default=100, le=500)):
        """Get Senate-only trades."""
        from civicledger.congress.trades import fetch_senate_trades
        data = await fetch_senate_trades(year=year, limit=limit)
        return {"chamber": "senate", "count": len(data), "trades": data}

    @app.get("/congress/house")
    async def get_house_trades(year: Optional[int] = None, limit: int = Query(default=100, le=500)):
        """Get House-only trades."""
        from civicledger.congress.trades import fetch_house_trades
        data = await fetch_house_trades(year=year, limit=limit)
        return {"chamber": "house", "count": len(data), "trades": data}

    # ── Economic Events ──

    @app.get("/economic-events")
    async def get_economic_events(
        from_date: str = Query(default=None),
        to_date: str = Query(default=None),
    ):
        """Get economic calendar events from FRED."""
        from civicledger.economic.fred import fetch_economic_events
        today = date.today()
        f = from_date or today.replace(day=1).isoformat()
        t = to_date or today.isoformat()
        data = await fetch_economic_events(f, t)
        return {
            "from_date": f,
            "to_date": t,
            "count": len(data),
            "events": data,
            "attribution": "This product uses the FRED API but is not endorsed or certified by the Federal Reserve Bank of St. Louis.",
        }

    # ── Material Events ──

    @app.get("/material-events")
    async def get_material_events(
        from_date: str = Query(default=None),
        to_date: str = Query(default=None),
        item: Optional[str] = Query(default=None, description="Filter by 8-K item code (e.g., 5.02 for officer changes)"),
    ):
        """Get material corporate events from 8-K filings."""
        from civicledger.edgar.material_events import fetch_material_events
        today = date.today()
        f = from_date or (today - timedelta(days=7)).isoformat()
        t = to_date or today.isoformat()
        data = await fetch_material_events(f, t, item_filter=item)
        return {"from_date": f, "to_date": t, "count": len(data), "events": data}

    return app


# For `uvicorn civicledger.api.server:app`
app = create_app()
