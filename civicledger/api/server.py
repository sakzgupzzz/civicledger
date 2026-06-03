"""FastAPI server for CivicLedger.

Run: civicledger serve --port 8080
Or:  uvicorn civicledger.api.server:app --reload

All data from US government public domain sources.
"""

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from loguru import logger

from civicledger.cache import cached
from civicledger.economic.fred import FredApiKeyMissing
from civicledger.validation import (
    ValidationError,
    normalize_8k_item,
    normalize_date_range,
    normalize_limit,
    normalize_ticker,
    normalize_year,
)

_STATIC_DIR = Path(__file__).parent / "static"

# Cache TTLs (seconds) — tuned to how often each source actually changes.
_TTL_FUNDAMENTALS = 21600  # 6h (quarterly data)
_TTL_INSTITUTIONS = 21600  # 6h (13F is quarterly)
_TTL_CONGRESS = 3600       # 1h
_TTL_EARNINGS = 3600       # 1h
_TTL_INSIDER = 1800        # 30m
_TTL_MATERIAL = 3600       # 1h
_TTL_ECONOMIC = 21600      # 6h

ATTRIBUTION = (
    "This product uses the FRED API but is not endorsed or certified by the "
    "Federal Reserve Bank of St. Louis."
)


def _bad_request(e: ValidationError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(e))


def _upstream_error(name: str, e: Exception) -> HTTPException:
    logger.opt(exception=True).error(f"{name} failed: {e}")
    return HTTPException(status_code=502, detail=f"Upstream data source error in {name}.")


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

    @app.get("/", include_in_schema=False)
    async def dashboard():
        """Serve the CivicLedger web dashboard."""
        return FileResponse(_STATIC_DIR / "dashboard.html")

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "version": "0.1.0",
            "sources": ["SEC EDGAR", "FRED", "House Clerk"],
        }

    # ── Fundamentals ──

    @app.get("/fundamentals")
    async def get_fundamentals(ticker: Optional[str] = None):
        """Get XBRL financial metrics for all or a specific company."""
        from civicledger.edgar.fundamentals import (
            fetch_fundamentals,
            fetch_fundamentals_for_ticker,
        )
        try:
            tk = normalize_ticker(ticker) if ticker else None
        except ValidationError as e:
            raise _bad_request(e)
        if tk:
            # Single-ticker fast path: one companyfacts call, not the bulk fetch.
            try:
                result = await cached(
                    f"fundamentals/{tk}", _TTL_FUNDAMENTALS,
                    lambda: fetch_fundamentals_for_ticker(tk),
                )
            except Exception as e:  # noqa: BLE001
                raise _upstream_error("fundamentals", e)
            if not result:
                raise HTTPException(status_code=404, detail=f"No fundamentals for {tk}.")
            return {"ticker": tk, **result}
        try:
            data = await fetch_fundamentals()
        except Exception as e:  # noqa: BLE001
            raise _upstream_error("fundamentals", e)
        keys = list(data.keys())
        return {
            "count": len(data),
            "tickers": keys[:50],
            "sample": {k: data[k] for k in keys[:5]},
        }

    @app.get("/fundamentals/{ticker}")
    async def get_ticker_fundamentals(ticker: str):
        """Get fundamentals for a specific ticker."""
        return await get_fundamentals(ticker=ticker)

    # ── Earnings ──

    @app.get("/earnings")
    async def get_earnings(
        from_date: str = Query(default=None),
        to_date: str = Query(default=None),
    ):
        """Get earnings announcements from 8-K Item 2.02 filings."""
        from civicledger.edgar.earnings import fetch_earnings
        try:
            f, t = normalize_date_range(from_date, to_date, default_lookback_days=7)
        except ValidationError as e:
            raise _bad_request(e)
        try:
            data = await cached(f"earnings/{f}/{t}", _TTL_EARNINGS, lambda: fetch_earnings(f, t))
        except Exception as e:  # noqa: BLE001
            raise _upstream_error("earnings", e)
        return {"from_date": f, "to_date": t, "count": len(data), "earnings": data}

    # ── Insider Trades ──

    @app.get("/insider-trades")
    async def get_insider_trades(
        from_date: str = Query(default=None),
        to_date: str = Query(default=None),
        ticker: Optional[str] = Query(default=None),
        limit: int = Query(default=200, le=1000),
    ):
        """Get insider trades from Form 4 filings."""
        from civicledger.edgar.insider_trades import fetch_recent_insider_trades
        try:
            f, t = normalize_date_range(from_date, to_date, default_lookback_days=7, max_span_days=92)
            tk = normalize_ticker(ticker) if ticker else None
        except ValidationError as e:
            raise _bad_request(e)
        lim = normalize_limit(limit, default=200)
        try:
            data = await cached(
                f"insider/{f}/{t}/{tk}/{lim}", _TTL_INSIDER,
                lambda: fetch_recent_insider_trades(f, t, ticker=tk, limit=lim),
            )
        except Exception as e:  # noqa: BLE001
            raise _upstream_error("insider-trades", e)
        return {"from_date": f, "to_date": t, "count": len(data), "trades": data}

    @app.get("/insider-trades/{ticker}")
    async def get_ticker_insider_trades(ticker: str, limit: int = Query(default=50, le=500)):
        """Get detailed insider trades for a specific ticker."""
        from civicledger.edgar.insider_trades import fetch_insider_trades_detailed
        try:
            tk = normalize_ticker(ticker)
        except ValidationError as e:
            raise _bad_request(e)
        lim = normalize_limit(limit, default=50, maximum=500)
        try:
            data = await cached(
                f"insider-detailed/{tk}/{lim}", _TTL_INSIDER,
                lambda: fetch_insider_trades_detailed(tk, limit=lim),
            )
        except Exception as e:  # noqa: BLE001
            raise _upstream_error("insider-trades-detailed", e)
        return {"ticker": tk, "count": len(data), "trades": data}

    # ── Institutional Holdings (13F) ──

    @app.get("/institutions")
    async def get_institutions():
        """Get summary of top institutional investors."""
        from civicledger.edgar.institutional import fetch_top_institutions_summary
        try:
            data = await cached("institutions/top", _TTL_INSTITUTIONS, fetch_top_institutions_summary)
        except Exception as e:  # noqa: BLE001
            raise _upstream_error("institutions", e)
        return {"count": len(data), "institutions": data}

    @app.get("/institutions/{manager}")
    async def get_institution_holdings(manager: str, limit: int = Query(default=100, le=500)):
        """Get 13F holdings for a specific institutional manager."""
        from civicledger.edgar.institutional import fetch_holdings
        lim = normalize_limit(limit, maximum=500)
        try:
            data = await cached(
                f"holdings/{manager}/{lim}", _TTL_INSTITUTIONS,
                lambda: fetch_holdings(manager, limit=lim),
            )
        except Exception as e:  # noqa: BLE001
            raise _upstream_error("institution-holdings", e)
        if isinstance(data, dict) and data.get("error"):
            raise HTTPException(status_code=404, detail=data["error"])
        return data

    # ── Congressional Trades ──

    @app.get("/congress")
    async def get_congressional_trades(
        year: Optional[int] = None,
        limit: int = Query(default=200, le=500),
        detailed: bool = Query(default=False, description="Parse PTR PDFs for real ticker/amount detail (slower)."),
    ):
        """Get congressional stock trades from the House clerk (House only)."""
        from civicledger.congress.trades import fetch_all_congressional_trades
        try:
            yr = normalize_year(year)
        except ValidationError as e:
            raise _bad_request(e)
        lim = normalize_limit(limit, default=200, maximum=500)
        try:
            data = await cached(
                f"congress/{yr}/{lim}/{detailed}", _TTL_CONGRESS,
                lambda: fetch_all_congressional_trades(year=yr, limit=lim, detailed=detailed),
            )
        except Exception as e:  # noqa: BLE001
            raise _upstream_error("congress", e)
        return {
            "year": yr,
            "chamber": "house",
            "detailed": detailed,
            "count": len(data),
            "trades": data,
            "note": "Senate trades are unavailable — the Senate eFD site blocks automated access.",
        }

    @app.get("/congress/senate")
    async def get_senate_trades():
        """Senate trades are not available via automation."""
        raise HTTPException(
            status_code=501,
            detail=(
                "Senate trade data is unavailable: the Senate eFD site "
                "(efdsearch.senate.gov) blocks automated access. Only House "
                "data is available — use /congress or /congress/house."
            ),
        )

    @app.get("/congress/house")
    async def get_house_trades(
        year: Optional[int] = None,
        limit: int = Query(default=100, le=500),
        detailed: bool = Query(default=False),
    ):
        """Get House trades. detailed=True parses PTR PDFs for ticker/amount."""
        from civicledger.congress.trades import (
            fetch_house_trades,
            fetch_house_trades_detailed,
        )
        try:
            yr = normalize_year(year)
        except ValidationError as e:
            raise _bad_request(e)
        lim = normalize_limit(limit, default=100, maximum=500)
        factory = (
            (lambda: fetch_house_trades_detailed(year=yr, limit=lim))
            if detailed
            else (lambda: fetch_house_trades(year=yr, limit=lim))
        )
        try:
            data = await cached(f"house/{yr}/{lim}/{detailed}", _TTL_CONGRESS, factory)
        except Exception as e:  # noqa: BLE001
            raise _upstream_error("congress-house", e)
        return {"chamber": "house", "year": yr, "detailed": detailed, "count": len(data), "trades": data}

    # ── Economic Events ──

    @app.get("/economic-events")
    async def get_economic_events(
        from_date: str = Query(default=None),
        to_date: str = Query(default=None),
    ):
        """Get economic calendar events from FRED."""
        from civicledger.economic.fred import fetch_economic_events
        try:
            f, t = normalize_date_range(from_date, to_date, default_lookback_days=0)
        except ValidationError as e:
            raise _bad_request(e)
        try:
            data = await cached(f"economic/{f}/{t}", _TTL_ECONOMIC, lambda: fetch_economic_events(f, t))
        except FredApiKeyMissing as e:
            raise HTTPException(status_code=503, detail=str(e))
        except Exception as e:  # noqa: BLE001
            raise _upstream_error("economic-events", e)
        return {
            "from_date": f,
            "to_date": t,
            "count": len(data),
            "events": data,
            "attribution": ATTRIBUTION,
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
        try:
            f, t = normalize_date_range(from_date, to_date, default_lookback_days=7)
            item_code = normalize_8k_item(item)
        except ValidationError as e:
            raise _bad_request(e)
        try:
            data = await cached(
                f"material/{f}/{t}/{item_code}", _TTL_MATERIAL,
                lambda: fetch_material_events(f, t, item_filter=item_code),
            )
        except Exception as e:  # noqa: BLE001
            raise _upstream_error("material-events", e)
        return {"from_date": f, "to_date": t, "item": item_code, "count": len(data), "events": data}

    return app


# For `uvicorn civicledger.api.server:app`
app = create_app()
