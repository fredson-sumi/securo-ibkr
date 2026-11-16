"""Mapping tests, ported from Securo's test_providers_ibkr.py. Fake data only (D3)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from tests.fake_report import report_xml

from securo_ibkr.flex import ParsedReport, parse_report
from securo_ibkr.mapping import (
    Position,
    cash_accounts,
    cash_transactions,
    positions,
    trade_from_execution,
    trades_for,
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


def _trade_report() -> ParsedReport:
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
        positions=[],
    )


def _position(external_id: str, ticker: str, conid: str | None = None) -> Position:
    return Position(
        external_id=external_id,
        name=ticker,
        currency="USD",
        current_value=Decimal("100"),
        quantity=None,
        unit_price=None,
        purchase_price=None,
        purchase_date=None,
        isin=None,
        ticker=ticker,
        metadata={"conid": conid} if conid else {},
    )


def test_trades_keyed_by_position_from_statement_of_funds():
    trades = trades_for(
        _trade_report(), [_position("exmp", "EXMP", "100001"), _position("othr", "OTHR")]
    )

    assert set(trades) == {"exmp", "othr"}
    assert trades["othr"] == []
    [trade] = trades["exmp"]
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
    trades = trades_for(report, [_position("a", "EXMP", "100001")])
    assert [t.external_id for t in trades["a"]] == ["trade:TR-1"]

    # Same ticker on two positions and no conid: the key maps nowhere.
    ambiguous = trades_for(_trade_report_by_symbol(), [_position("a", "EXMP"), _position("b", "EXMP")])
    assert ambiguous == {"a": [], "b": []}


def _trade_report_by_symbol() -> ParsedReport:
    report = _trade_report()
    del report.statement_funds[0]["conid"]
    return report


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
    [trade] = trades_for(report, [_position("a", "EXMP", "100001")])["a"]
    assert (trade.external_id, trade.kind, trade.quantity, trade.price, trade.fee) == (
        "trade:EX-1",
        "sell",
        Decimal("1000"),
        Decimal("3.5"),
        Decimal("1.25"),
    )


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


def test_execution_missing_id_is_skipped():
    assert trade_from_execution({"conid": "1", "quantity": "1", "tradePrice": "1"}) is None
