"""Database models for CivicLedger."""

from datetime import datetime
from sqlalchemy import (
    Column, DateTime, Float, Integer, Numeric, String, Text, Index,
)
from sqlalchemy.sql import func

from civicledger.db import Base


class Company(Base):
    """Public company from SEC EDGAR."""

    __tablename__ = "companies"

    cik = Column(Integer, primary_key=True)
    ticker = Column(String(20), unique=True, nullable=True, index=True)
    name = Column(String(500), nullable=False)
    sic = Column(String(10), nullable=True)
    sector = Column(String(100), nullable=True, index=True)
    industry = Column(String(200), nullable=True)
    state = Column(String(10), nullable=True)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class Fundamental(Base):
    """Quarterly/annual financial metrics from XBRL."""

    __tablename__ = "fundamentals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    cik = Column(Integer, nullable=False, index=True)
    ticker = Column(String(20), nullable=True, index=True)
    period = Column(String(10), nullable=False)  # CY2025Q1, CY2024

    # Income statement
    revenue = Column(Numeric(20, 2), nullable=True)
    net_income = Column(Numeric(20, 2), nullable=True)
    gross_profit = Column(Numeric(20, 2), nullable=True)
    operating_income = Column(Numeric(20, 2), nullable=True)
    eps = Column(Numeric(10, 4), nullable=True)

    # Balance sheet
    total_assets = Column(Numeric(20, 2), nullable=True)
    total_liabilities = Column(Numeric(20, 2), nullable=True)
    stockholders_equity = Column(Numeric(20, 2), nullable=True)
    current_assets = Column(Numeric(20, 2), nullable=True)
    current_liabilities = Column(Numeric(20, 2), nullable=True)
    inventory = Column(Numeric(20, 2), nullable=True)
    long_term_debt = Column(Numeric(20, 2), nullable=True)
    shares_outstanding = Column(Numeric(20, 0), nullable=True)
    dividends_per_share = Column(Numeric(10, 4), nullable=True)

    # Computed ratios
    profit_margin = Column(Float, nullable=True)
    gross_margin = Column(Float, nullable=True)
    operating_margin = Column(Float, nullable=True)
    return_on_equity = Column(Float, nullable=True)
    return_on_assets = Column(Float, nullable=True)
    current_ratio = Column(Float, nullable=True)
    quick_ratio = Column(Float, nullable=True)
    debt_to_equity = Column(Float, nullable=True)
    revenue_growth = Column(Float, nullable=True)
    earnings_growth = Column(Float, nullable=True)

    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_fundamentals_ticker_period", "ticker", "period", unique=True),
    )


class EarningsAnnouncement(Base):
    """Earnings announcement from 8-K Item 2.02."""

    __tablename__ = "earnings_announcements"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=True, index=True)
    company = Column(String(500), nullable=True)
    filing_date = Column(String(10), nullable=False, index=True)
    cik = Column(Integer, nullable=True)
    accession_number = Column(String(30), nullable=True)

    __table_args__ = (
        Index("ix_earnings_ticker_date", "ticker", "filing_date"),
    )


class InsiderTrade(Base):
    """Insider transaction from SEC Form 4."""

    __tablename__ = "insider_trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=True, index=True)
    company = Column(String(500), nullable=True)
    cik = Column(Integer, nullable=True)

    # Insider info
    insider_name = Column(String(300), nullable=False)
    insider_title = Column(String(200), nullable=True)
    insider_cik = Column(Integer, nullable=True)

    # Transaction
    transaction_date = Column(String(10), nullable=False, index=True)
    transaction_type = Column(String(50), nullable=True)  # Purchase, Sale, Grant, etc.
    shares = Column(Numeric(20, 4), nullable=True)
    price_per_share = Column(Numeric(12, 4), nullable=True)
    total_value = Column(Numeric(20, 2), nullable=True)
    shares_owned_after = Column(Numeric(20, 4), nullable=True)

    # Filing
    filing_date = Column(String(10), nullable=True)
    accession_number = Column(String(30), nullable=True)

    __table_args__ = (
        Index("ix_insider_ticker_date", "ticker", "transaction_date"),
    )


class InstitutionalHolding(Base):
    """Institutional holding from SEC 13F."""

    __tablename__ = "institutional_holdings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    manager_cik = Column(Integer, nullable=False, index=True)
    manager_name = Column(String(500), nullable=False)

    # Holding
    ticker = Column(String(20), nullable=True, index=True)
    company = Column(String(500), nullable=True)
    cusip = Column(String(9), nullable=True)
    shares = Column(Numeric(20, 0), nullable=True)
    value_thousands = Column(Numeric(20, 0), nullable=True)  # 13F reports in $1000s
    share_change = Column(Numeric(20, 0), nullable=True)  # change from prior quarter
    change_percent = Column(Float, nullable=True)

    # Period
    period = Column(String(10), nullable=False)  # 2025-03-31
    filing_date = Column(String(10), nullable=True)

    __table_args__ = (
        Index("ix_inst_manager_ticker_period", "manager_cik", "ticker", "period"),
    )


class CongressionalTrade(Base):
    """Congressional stock trade from Senate eFD / House clerk."""

    __tablename__ = "congressional_trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    politician = Column(String(200), nullable=False, index=True)
    chamber = Column(String(10), nullable=True)  # senate, house
    party = Column(String(20), nullable=True)
    state = Column(String(5), nullable=True)

    # Trade
    ticker = Column(String(20), nullable=True, index=True)
    asset_description = Column(Text, nullable=True)
    transaction_type = Column(String(50), nullable=True)  # purchase, sale
    transaction_date = Column(String(10), nullable=True, index=True)
    disclosure_date = Column(String(10), nullable=True)
    amount_range = Column(String(50), nullable=True)  # "$1,001 - $15,000"

    # Filing
    source_url = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_congress_politician_date", "politician", "transaction_date"),
    )


class EconomicEvent(Base):
    """Economic calendar event from FRED."""

    __tablename__ = "economic_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False)
    date = Column(String(10), nullable=False, index=True)
    impact = Column(String(10), nullable=False)  # high, medium, low
    description = Column(Text, nullable=True)
    source = Column(String(50), default="FRED")


class MaterialEvent(Base):
    """Material corporate event from 8-K filings."""

    __tablename__ = "material_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=True, index=True)
    company = Column(String(500), nullable=True)
    cik = Column(Integer, nullable=True)
    filing_date = Column(String(10), nullable=False, index=True)
    items = Column(String(200), nullable=True)  # "2.02,9.01"
    title = Column(Text, nullable=True)
    accession_number = Column(String(30), nullable=True)
    filing_url = Column(Text, nullable=True)
