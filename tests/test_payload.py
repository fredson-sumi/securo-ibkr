"""What `sync` hands Securo: contract 1 records. Fake data only (D3)."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from tests.fake_report import report_xml
from tests.test_mapping import _open, _trade_report

from securo_ibkr.flex import parse_report
from securo_ibkr.payload import INSTITUTION_LOGO, build, number

START, END = date(2025, 11, 16), date(2026, 11, 15)


def _by_id(records: list[dict]) -> dict[str, dict]:
    return {r["external_id"]: r for r in records}


def test_accounts_carry_cash_and_its_activity():
    data = build(parse_report(report_xml()), START, END, {})
    assert data["contract"] == 1
    usd = _by_id(data["accounts"])["U1234567:USD"]
    assert (usd["type"], usd["balance"], usd["currency"]) == ("investment", "1000.25", "USD")
    assert "institution_name" not in usd  # it would replace each account's name in Securo
    assert usd["masked_number"] == "4567" and usd["institution_logo"] == "ibkr"
    assert {t["external_id"]: (t["type"], t["amount"]) for t in usd["transactions"]} == {
        "ibkr:U1234567:USD:TX-DIV": ("credit", "12.34"),
        "ibkr:U1234567:USD:TX-FEE": ("debit", "5"),
    }
    eur = _by_id(data["accounts"])["U1234567:EUR"]
    assert eur["balance"] == "-50.5" and [t["date"] for t in eur["transactions"]] == ["2026-08-09"]


def test_the_logo_accounts_name_is_a_small_png_the_package_ships():
    root = Path(__file__).resolve().parent.parent
    logos = json.loads((root / "securo-plugin.json").read_text())["logos"]
    image = (root / logos[INSTITUTION_LOGO]).read_bytes()  # Securo refuses a key the manifest lacks
    assert image.startswith(b"\x89PNG\r\n\x1a\n") and len(image) <= 256 * 1024
    data = build(parse_report(report_xml()), START, END, {})
    assert all("institution_logo_url" not in account for account in data["accounts"])  # never a URL (D26)


def test_open_positions_are_holdings_and_shorts_have_no_trades():
    data = build(parse_report(report_xml()), START, END, {})
    holdings = _by_id(data["holdings"])
    long = holdings["U1234567:Core:100001"]
    assert (long["quantity"], long["current_value"], long["purchase_price"], long["isin"]) == ("3", "660", "500", "US0000000001")
    assert long["unit_price"] == "220" and "raw_data" not in long["metadata"]
    assert long["type"] == "stock"
    short = holdings["U1234567:default:100002"]
    assert short["quantity"] == "-100" and short["trades"] is None


def test_the_first_statement_opens_what_was_held_before_it():
    state: dict = {}
    data = build(parse_report(report_xml()), START, END, state)
    long = _by_id(data["holdings"])["U1234567:Core:100001"]
    # 3 held and no execution in the statement: all 3 were held before it, at IBKR's cost.
    (opening,) = long["trades"]
    assert opening == {"external_id": "opening:U1234567:Core:100001", "kind": "buy", "date": "2025-11-15",
                       "quantity": "3", "price": "166.66666667", "fee": "0"}
    assert state["openings"]["U1234567:Core:100001"] == opening


def test_later_statements_resend_the_opening_and_invent_no_new_one():
    state = {"last_report_end": "2026-11-10", "openings": {"U1234567:Core:100001": {"external_id": "opening:x"}}}
    data = build(parse_report(report_xml()), date(2026, 11, 11), END, state)
    long = _by_id(data["holdings"])["U1234567:Core:100001"]
    assert long["trades"][0] == {"external_id": "opening:x"}
    report = _trade_report(_open("100009", "OTHR"))
    other = _by_id(build(report, date(2026, 11, 11), END, state)["holdings"])["U1234567:default:100009"]
    assert other["trades"] == []


def test_a_sell_older_holdings_cover_gets_an_opening_before_it():
    report = _trade_report(_open("100001", "EXMP"))
    report.trades.extend([
        {"conid": "100001", "tradeID": "S1", "tradeDate": "20260301", "quantity": "-4", "tradePrice": "10",
         "buySell": "SELL", "currency": "USD", "multiplier": "1"},
        {"conid": "100001", "tradeID": "B1", "tradeDate": "20260401", "quantity": "4", "tradePrice": "11",
         "buySell": "BUY", "currency": "USD", "multiplier": "1"},
    ])
    trades = _by_id(build(report, START, END, {})["holdings"])["U1234567:default:100001"]["trades"]
    assert [(t["external_id"], t["quantity"]) for t in trades] == [
        ("opening:U1234567:default:100001", "4"), ("trade:S1", "4"), ("trade:B1", "4"),
    ]


def test_instruments_closed_within_the_statement_end_at_zero_with_their_trades():
    report = _trade_report()
    base = {"conid": "200001", "symbol": "GONE", "description": "GONE INC", "assetCategory": "STK",
            "currency": "USD", "multiplier": "1"}
    report.trades.extend([
        {**base, "tradeID": "B1", "tradeDate": "20260301", "quantity": "5", "tradePrice": "10", "buySell": "BUY"},
        {**base, "tradeID": "S1", "tradeDate": "20260401", "quantity": "-5", "tradePrice": "12", "buySell": "SELL"},
    ])
    gone = _by_id(build(report, START, END, {})["holdings"])["U1234567:default:200001"]
    assert (gone["quantity"], gone["current_value"]) == ("0", "0")
    assert [t["external_id"] for t in gone["trades"]] == ["trade:B1", "trade:S1"]


def test_a_sold_out_instrument_is_listed_by_every_later_statement_until_it_is_held_again():
    state: dict = {}
    report = _trade_report()
    base = {"conid": "200001", "symbol": "GONE", "description": "GONE INC", "assetCategory": "STK",
            "currency": "USD", "multiplier": "1"}
    report.trades.extend([
        {**base, "tradeID": "B1", "tradeDate": "20260301", "quantity": "5", "tradePrice": "10", "buySell": "BUY"},
        {**base, "tradeID": "S1", "tradeDate": "20260401", "quantity": "-5", "tradePrice": "12", "buySell": "SELL"},
    ])
    build(report, START, END, state)
    state["last_report_end"] = END.isoformat()

    # The next statement doesn't mention it: Securo would archive a holding that isn't listed.
    day = date(2026, 11, 16)
    gone = _by_id(build(_trade_report(), day, day, state)["holdings"])["U1234567:default:200001"]
    assert (gone["name"], gone["type"], gone["quantity"], gone["current_value"]) == ("GONE INC", "stock", "0", "0")
    assert gone["trades"] is None  # the trades Securo already keeps stay as they are

    held = build(_trade_report(_open("200001", "GONE")), day, day, state)["holdings"]
    assert [h["quantity"] for h in held if h["external_id"] == "U1234567:default:200001"] == ["3"]
    assert state["closed"] == {}


def _first_statement_trades(position: dict, *executions: dict) -> list[dict]:
    report = _trade_report({**_open("100001", "EXMP"), **position})
    report.trades.extend(
        {"conid": "100001", "currency": "USD", "multiplier": "1", **execution} for execution in executions
    )
    return _by_id(build(report, START, END, {})["holdings"])["U1234567:default:100001"]["trades"]


def test_the_opening_costs_what_the_units_held_before_the_statement_cost():
    # 10 held before at 100 each. The statement bought 10 more at 200: 20 held, cost basis 3000.
    opening = _first_statement_trades(
        {"position": "20", "costBasisMoney": "3000"},
        {"tradeID": "B1", "tradeDate": "20260601", "quantity": "10", "tradePrice": "200", "buySell": "BUY"},
    )[0]
    assert (opening["quantity"], opening["price"]) == ("10", "100")

    # 10 held before at 100 each. The statement sold 4 at 150, realizing 200: 6 held, cost basis 600.
    opening = _first_statement_trades(
        {"position": "6", "costBasisMoney": "600"},
        {"tradeID": "S1", "tradeDate": "20260601", "quantity": "-4", "tradePrice": "150", "buySell": "SELL",
         "fifoPnlRealized": "200"},
    )[0]
    assert (opening["quantity"], opening["price"]) == ("10", "100")


def test_a_sell_that_does_not_say_what_it_realized_leaves_the_average_cost():
    # 15 held, cost basis 2500, after a buy of 10 and a sell of 5 without its realized gain.
    opening = _first_statement_trades(
        {"position": "15", "costBasisMoney": "2500"},
        {"tradeID": "B1", "tradeDate": "20260601", "quantity": "10", "tradePrice": "200", "buySell": "BUY"},
        {"tradeID": "S1", "tradeDate": "20260701", "quantity": "-5", "tradePrice": "210", "buySell": "SELL"},
    )[0]
    assert (opening["quantity"], opening["price"]) == ("10", "166.66666667")


def test_an_isin_the_contract_would_refuse_is_left_out():
    report = _trade_report({**_open("100001", "EXMP"), "isin": "US-000000001"})
    assert "isin" not in build(report, START, END, {})["holdings"][0]
    report = _trade_report({**_open("100001", "EXMP"), "isin": "us0000000001"})
    assert build(report, START, END, {})["holdings"][0]["isin"] == "US0000000001"


def test_numbers_are_plain_strings_with_at_most_eight_decimals():
    from decimal import Decimal

    assert number(Decimal("1E+3")) == "1000"
    assert number(Decimal("0.123456789")) == "0.12345679"
    assert number(Decimal("-50.50")) == "-50.5"
