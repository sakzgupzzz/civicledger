"""Offline tests for Form 4 ownership-summary -> trade rows (no network)."""

from types import SimpleNamespace

from civicledger.edgar.insider_trades import _summary_to_transactions, _to_float, _to_int


def _txn(**kw):
    base = dict(transaction_type=None, code=None, shares=None, value=None,
               price_per_share=None, security_title=None, security_type=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_summary_expands_each_transaction():
    summary = SimpleNamespace(
        insider_name="Arthur D Levinson",
        issuer_name="Apple Inc.",
        issuer_ticker="AAPL",
        position="Director",
        reporting_date="2026-05-27",
        remaining_shares=3764576,
        primary_activity="Sale",
        transactions=[
            _txn(transaction_type="sale", code="S", shares=50000,
                 value=15551000.0, price_per_share=311.02,
                 security_title="Common Stock", security_type="non-derivative"),
            _txn(transaction_type="gift", code="G", shares=65000,
                 value=0, price_per_share=0.0, security_title="Common Stock"),
        ],
    )
    rows = _summary_to_transactions(summary)
    assert len(rows) == 2
    sale = rows[0]
    assert sale["ticker"] == "AAPL"
    assert sale["insider_name"] == "Arthur D Levinson"
    assert sale["insider_title"] == "Director"
    assert sale["transaction_code"] == "S"
    assert sale["shares"] == 50000
    assert sale["price_per_share"] == 311.02
    assert sale["total_value"] == 15551000.0
    assert sale["shares_owned_after"] == 3764576


def test_footnote_markers_coerce_to_none():
    assert _to_float("[F1]") is None
    assert _to_int("[F2]") is None
    assert _to_float("311.02") == 311.02
    assert _to_int("50000") == 50000
    assert _to_float(None) is None


def test_filing_with_no_transactions_still_emits_row():
    summary = SimpleNamespace(
        insider_name="Jane Doe", issuer_name="Foo Corp", issuer_ticker="FOO",
        position="CFO", reporting_date="2026-01-02", remaining_shares=None,
        primary_activity="Sale", transactions=[],
    )
    rows = _summary_to_transactions(summary)
    assert len(rows) == 1
    assert rows[0]["ticker"] == "FOO"
    assert rows[0]["transaction_type"] == "Sale"
    assert rows[0]["shares"] is None
