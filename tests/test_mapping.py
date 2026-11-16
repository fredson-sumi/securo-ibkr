"""Mapping tests, ported from Securo's test_providers_ibkr.py. Fake data only (D3)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from tests.fake_report import report_xml

from securo_ibkr.flex import ParsedReport, parse_report
from securo_ibkr.mapping import (
    cash_accounts,
    cash_transactions,
    holdings,
    positions,
    trade_from_execution,
    trade_from_statement_fund,
)


@pytest.fixture
def report() -> ParsedReport:
    return parse_report(report_xml())


# --- ported from test_maps_currency_accounts_non_trade_activity_and_positions


def test_cash_accounts_per_currency(report):
    accounts = cash_accounts(report)

    assert [(a.currency, a.balance) for a in accounts] == [
        ("EUR", Decimal("-50.50")),
        ("USD", Decimal("1000.25")),
    ]
    assert accounts[1].external_id == "U1234567:USD"
    assert accounts[1].name == "Long Term Cash (USD)"


def test_cash_transactions_skip_trades_and_balance_rows(report):
    transactions = cash_transactions(report, "USD")

    assert [t.external_id for t in transactions] == [
        "ibkr:U1234567:USD:TX-DIV",
        "ibkr:U1234567:USD:TX-FEE",
    ]
    assert transactions[0].type == "credit"
    assert transactions[0].amount == Decimal("12.34")
    assert transactions[0].payee == "EXMP"
    assert transactions[0].description == "EXMP dividend"
    assert transactions[1].type == "debit"
    assert transactions[1].amount == Decimal("5.00")
    assert transactions[1].payee == "Interactive Brokers"
    assert transactions[0].raw_data["transactionID"] == "TX-DIV"


def test_positions(report):
    holdings = positions(report)

    assert len(holdings) == 2
    assert holdings[0].external_id == "U1234567:Core:100001"
    assert holdings[0].current_value == Decimal("660")
    assert holdings[0].quantity == Decimal("3")
    assert holdings[0].purchase_price == Decimal("500")
    assert holdings[0].purchase_date == date(2025, 1, 2)
    assert holdings[0].metadata["model"] == "Core"
    assert holdings[0].metadata["mark_price"] == "220"
    assert holdings[1].external_id == "U1234567:default:100002"
    assert holdings[1].quantity == Decimal("-1")
    assert holdings[1].current_value == Decimal("-350")
    assert holdings[1].metadata["model"] is None
    assert holdings[1].isin is None
    assert holdings[1].purchase_date is None


# --- added: cash activity filters --------------------------------------------


def test_cash_transactions_currency_is_case_insensitive_and_since_filters(report):
    eur = cash_transactions(report, "eur")
    assert [(t.external_id, t.amount, t.type) for t in eur] == [
        ("ibkr:U1234567:EUR:TX-INT", Decimal("1.25"), "credit")
    ]
    assert [t.external_id for t in cash_transactions(report, "USD", since=date(2026, 8, 11))] == [
        "ibkr:U1234567:USD:TX-FEE"
    ]
    assert cash_transactions(report, "GBP") == []


# --- ported from test_holding_trades_are_keyed_by_holding_from_the_full_window
# (the re-fetch half belongs to the fetch tests)


def _open(conid: str | None, symbol: str, *, model: str = "", multiplier: str = "1", currency: str = "USD") -> dict:
    return {
        "conid": conid or "", "symbol": symbol, "model": model, "currency": currency,
        "assetCategory": "STK", "description": symbol, "position": "3", "multiplier": multiplier,
        "markPrice": "220", "positionValue": "660", "costBasisMoney": "500",
    }


def _trade_report(*open_rows: dict) -> ParsedReport:
    return ParsedReport(
        account_id="U1234567",
        account_alias=None,
        account_name=None,
        base_currency="USD",
        cash_balances={},
        statement_funds=[
            {
                "currency": "USD",
                "date": "20260812",
                "transactionID": "TX-TRADE",
                "tradeID": "TR-1",
                "tradeQuantity": "2",
                "tradePrice": "200",
                "amount": "-401",
                "symbol": "EXMP",
                "conid": "100001",
            }
        ],
        positions=list(open_rows) or [_open("100001", "EXMP"), _open("100009", "OTHR")],
    )


def _by_id(report: ParsedReport) -> dict:
    return {h.external_id: h for h in holdings(report)}


def test_trades_keyed_by_position_from_statement_of_funds():
    found = _by_id(_trade_report())
    assert set(found) == {"U1234567:default:100001", "U1234567:default:100009"}
    assert found["U1234567:default:100009"].trades == []
    [trade] = found["U1234567:default:100001"].trades
    assert (trade.external_id, trade.kind, trade.quantity, trade.price, trade.fee) == (
        "trade:TR-1",
        "buy",
        Decimal("2"),
        Decimal("200"),
        Decimal("1"),
    )
    assert trade.date == date(2026, 8, 12)


# --- added: matching rules ---------------------------------------------------


def test_trades_skip_ambiguous_keys_other_currencies_and_duplicates():
    report = _trade_report()
    row = report.statement_funds[0]
    report.statement_funds.extend(
        [dict(row), {**row, "currency": "EUR", "tradeID": "TR-2"}]
    )
    trades = _by_id(report)["U1234567:default:100001"].trades
    assert [t.external_id for t in trades] == ["trade:TR-1"]

    # Same ticker in two models and no conid on the row: the key maps nowhere.
    report = _trade_report(_open("100001", "EXMP", model="A"), _open("100002", "EXMP", model="B"))
    del report.statement_funds[0]["conid"]
    assert all(h.trades == [] for h in holdings(report))


def test_statement_of_funds_trades_use_the_position_multiplier():
    """D13: an option's units and gross need the contract multiplier."""
    report = _trade_report(_open("100001", "EXMP", multiplier="100"))
    report.statement_funds[0].update({"tradeQuantity": "-1", "tradePrice": "3.5", "amount": "348.75"})
    [trade] = holdings(report)[0].trades
    assert (trade.kind, trade.quantity, trade.price, trade.fee) == (
        "sell", Decimal("100"), Decimal("3.5"), Decimal("1.25"),
    )
    # Without the multiplier, the fee came out as 350 - 348.75 - ... about 99x too high.
    _, unscaled = trade_from_statement_fund(report.statement_funds[0])
    assert unscaled.quantity == Decimal("1")


def test_trades_section_preferred_over_statement_of_funds():
    report = _trade_report()
    report.trades.append(
        {
            "conid": "100001",
            "tradeID": "EX-1",
            "tradeDate": "20260812",
            "quantity": "-10",
            "tradePrice": "3.5",
            "multiplier": "100",
            "proceeds": "3500",
            "netCash": "3498.75",
            "currency": "USD",
        }
    )
    [trade] = _by_id(report)["U1234567:default:100001"].trades
    assert (trade.external_id, trade.kind, trade.quantity, trade.price, trade.fee) == (
        "trade:EX-1",
        "sell",
        Decimal("1000"),
        Decimal("3.5"),
        Decimal("1.25"),
    )


def test_traded_and_closed_instruments_become_closed_holdings():
    report = _trade_report()
    base = {"conid": "200001", "symbol": "GONE", "description": "GONE INC", "assetCategory": "STK",
            "currency": "USD", "multiplier": "1"}
    report.trades.extend([
        {**base, "tradeID": "B1", "tradeDate": "20260301", "quantity": "5", "tradePrice": "10", "buySell": "BUY"},
        {**base, "tradeID": "S1", "tradeDate": "20260401", "quantity": "-5", "tradePrice": "12", "buySell": "SELL",
         "fifoPnlRealized": "9", "openCloseIndicator": "C"},
        {**base, "conid": "300001", "assetCategory": "CASH", "symbol": "EUR.USD", "tradeID": "FX1",
         "tradeDate": "20260401", "quantity": "100", "tradePrice": "1.1"},
    ])
    found = _by_id(report)
    gone = found["U1234567:default:200001"]
    assert gone.position is None and gone.units == 0 and gone.name == "GONE INC"
    assert [t.kind for t in gone.trades] == ["buy", "sell"]
    assert gone.trades[1].realized == Decimal("9")
    assert "U1234567:default:300001" not in found  # currency conversions aren't holdings


def test_short_positions_and_short_opening_sells_are_flagged(report):
    found = _by_id(report)
    assert found["U1234567:default:100002"].is_short
    assert found["U1234567:Core:100001"].units == Decimal("3")
    assert found["U1234567:Core:100001"].type == "stock"


def test_execution_without_cash_fields_uses_commission_and_taxes():
    key, trade = trade_from_execution(
        {
            "conid": "100001",
            "tradeID": "EX-2",
            "tradeDate": "2026-08-12",
            "quantity": "5",
            "tradePrice": "10",
            "buySell": "BUY",
            "currency": "USD",
            "ibCommission": "-1",
            "ibCommissionCurrency": "USD",
            "taxes": "0.5",
        }
    )
    assert key == "100001"
    assert (trade.kind, trade.quantity, trade.price, trade.fee) == (
        "buy",
        Decimal("5"),
        Decimal("10"),
        Decimal("1.5"),
    )


def test_execution_without_cash_fields_keeps_the_per_unit_price_for_options():
    _, trade = trade_from_execution(
        {"conid": "1", "tradeID": "EX-3", "tradeDate": "2026-08-12", "quantity": "2",
         "tradePrice": "3.5", "multiplier": "100", "buySell": "BUY", "currency": "USD"}
    )
    assert (trade.quantity, trade.price) == (Decimal("200"), Decimal("3.5"))


def test_execution_missing_id_is_skipped():
    assert trade_from_execution({"conid": "1", "quantity": "1", "tradePrice": "1"}) is None
