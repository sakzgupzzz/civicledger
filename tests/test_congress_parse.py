"""Offline tests for House PTR PDF text parsing (no network)."""

from civicledger.congress.trades import parse_ptr_text

# Reflowed text as produced by pdfplumber from a real House PTR PDF: a single
# common-stock sale with a ticker, plus a municipal bond with no ticker.
STOCK_PTR = """\
Filing ID #20032062
Name: Hon. Robert B. Aderholt
Status: Member
State/District: AL04
ID Owner Asset Transaction Date Notification Amount Cap.
Type Date Gains >
$200?
GSK plc American Depositary Shares S 07/28/2025 08/11/2025 $1,001 - $15,000
(GSK) [ST]
F      S     : New
"""

BOND_PTR = """\
ID Owner Asset Transaction Date Notification Amount Cap.
Type Date Gains >
$200?
JT Arizona Indl Dev Auth Rev BDS S (partial) 06/11/2025 06/11/2025 $15,001 -
Lincoln 5.00% Due Nov 1, 2029 [GS] $50,000
F      S     : New
JT University Tex Univ Revs Fing SYS BDS P 06/04/2025 06/04/2025 $250,001 -
5.00% Due Aug 15, 2036 [GS] $500,000
F      S     : New
"""


def test_parses_stock_with_ticker():
    trades = parse_ptr_text(STOCK_PTR)
    assert len(trades) == 1
    t = trades[0]
    assert t["ticker"] == "GSK"
    assert t["transaction_type"] == "sale"
    assert t["transaction_date"] == "2025-07-28"
    assert t["notification_date"] == "2025-08-11"
    assert t["amount_range"] == "$1,001 - $15,000"
    assert t["asset_type"] == "ST"
    assert "GSK plc" in t["asset_description"]


def test_parses_bonds_without_ticker():
    trades = parse_ptr_text(BOND_PTR)
    assert len(trades) == 2
    sale, purchase = trades[0], trades[1]
    assert sale["ticker"] is None
    assert sale["transaction_type"] == "sale"
    assert sale["amount_range"] == "$15,001 - $50,000"
    assert purchase["transaction_type"] == "purchase"
    assert purchase["amount_range"] == "$250,001 - $500,000"
    # No table-header / filing-status noise leaks into the asset name.
    for t in trades:
        assert "$200?" not in (t["asset_description"] or "")
        assert "F S" not in (t["asset_description"] or "")


def test_empty_text_returns_empty():
    assert parse_ptr_text("") == []
    assert parse_ptr_text("no transactions here") == []
