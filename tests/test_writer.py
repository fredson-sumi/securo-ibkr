"""Writing a statement into Securo, against the in-memory fake API."""
from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from securo_ibkr.api import ApiError, OutOfTime, SecuroApi
from securo_ibkr.flex import ParsedReport, parse_report
from securo_ibkr.writer import write_report
from tests.fake_report import report_xml
from tests.fake_securo import BASE, TOKEN, FakeSecuro

START, END = date(2025, 11, 16), date(2026, 11, 15)
EXMP = "U1234567:Core:100001"
PUT = "U1234567:default:100002"


@pytest.fixture
def securo() -> FakeSecuro:
    return FakeSecuro()


def api_for(securo: FakeSecuro, **kwargs) -> SecuroApi:
    return SecuroApi(BASE, TOKEN, transport=securo, **kwargs)


def report() -> ParsedReport:
    return parse_report(report_xml())


def write(securo: FakeSecuro, state: dict, rpt: ParsedReport | None = None, start=START, end=END):
    return write_report(api_for(securo), rpt or report(), state, start, end)


def account(securo: FakeSecuro, currency: str) -> dict:
    [found] = [a for a in securo.accounts.values() if a["currency"] == currency]
    return securo.account_read(found)


def asset(securo: FakeSecuro, external_id: str) -> dict:
    [found] = [a for a in securo.assets.values() if a["external_id"] == external_id]
    return securo.asset_read(found)


# --- cash accounts ---------------------------------------------------------


def test_first_import_creates_one_account_per_currency_matching_ibkr_cash(securo):
    state: dict = {}
    summary = write(securo, state)
    assert summary.accounts_created == 2
    usd, eur = account(securo, "USD"), account(securo, "EUR")
    assert usd["name"] == "Long Term Cash (USD)" and usd["type"] == "investment"
    assert usd["current_balance"] == 1000.25 and eur["current_balance"] == -50.50
    # D10: the opening absorbs everything before the imported activity.
    assert usd["balance"] == pytest.approx(1000.25 - 12.34 + 5.00)
    assert usd["balance_date"] == "2025-11-15"
    assert set(state["accounts"]) == {"U1234567:USD", "U1234567:EUR"}


def test_second_import_of_the_same_statement_writes_nothing(securo):
    state: dict = {}
    write(securo, state)
    before = len(securo.calls)
    summary = write(securo, state)
    assert [c for c in securo.calls[before:] if c[0] != "GET"] == []
    assert summary.to_state().keys() <= {"assets", "skipped_short"}
    assert ("POST", "/assets") not in securo.calls[before:]


def test_deleted_account_is_recreated_and_lost_state_adopts_by_name(securo):
    state: dict = {}
    write(securo, state)
    usd_id = state["accounts"]["U1234567:USD"]["id"]

    lost: dict = {}
    write(securo, lost)
    assert lost["accounts"]["U1234567:USD"]["id"] == usd_id
    assert len(securo.accounts) == 2

    del securo.accounts[usd_id]
    securo.transactions = [t for t in securo.transactions if t["account_id"] != usd_id]
    write(securo, state)
    assert state["accounts"]["U1234567:USD"]["id"] != usd_id
    assert account(securo, "USD")["current_balance"] == 1000.25


def test_balance_follows_ibkr_when_later_statements_add_activity(securo):
    state: dict = {}
    write(securo, state)
    later = report()
    later.cash_balances["USD"] = Decimal("1100.25")
    later.statement_funds.append(
        {"currency": "USD", "date": "20261116", "transactionID": "TX-DEP", "activityCode": "DEP",
         "activityDescription": "Deposit", "amount": "100"}
    )
    write(securo, state, later, start=date(2026, 11, 16), end=date(2026, 11, 16))
    usd = account(securo, "USD")
    assert usd["current_balance"] == 1100.25
    assert usd["balance_date"] == "2025-11-15"  # the opening stays before all activity


# --- cash activity ---------------------------------------------------------


def test_cash_activity_is_imported_once_without_trades(securo):
    write(securo, {})
    rows = {(t["external_id"], t["type"], t["amount"]) for t in securo.transactions}
    assert rows == {
        ("ibkr:U1234567:USD:TX-DIV", "credit", "12.34"),
        ("ibkr:U1234567:USD:TX-FEE", "debit", "5.00"),
        ("ibkr:U1234567:EUR:TX-INT", "credit", "1.25"),
    }
    assert all(t["status"] == "posted" for t in securo.transactions)


def test_interrupted_import_resumes_without_duplicates(securo):
    state: dict = {}
    ticks = iter(range(100))
    api = api_for(securo, deadline=6, clock=lambda: next(ticks))
    with pytest.raises(OutOfTime):
        write_report(api, report(), state, START, END)
    assert 0 < len(securo.writes())
    write(securo, state)
    write(securo, state)
    assert len(securo.transactions) == 3
    assert len(securo.accounts) == 2
    assert account(securo, "USD")["current_balance"] == 1000.25
    assert asset(securo, EXMP)["units"] == 3


# --- assets ----------------------------------------------------------------


def test_open_positions_become_manual_assets_valued_at_the_statement_date(securo):
    write(securo, {})
    exmp = asset(securo, EXMP)
    assert (exmp["name"], exmp["type"], exmp["currency"], exmp["ticker"]) == ("EXAMPLE CORP", "stock", "USD", "EXMP")
    assert exmp["valuation_method"] == "manual"
    assert securo.values[exmp["id"]] == [
        {"id": securo.values[exmp["id"]][0]["id"], "asset_id": exmp["id"], "amount": 660.0, "date": "2026-11-15", "source": "manual"}
    ]
    put = asset(securo, PUT)
    assert put["ticker"] is None and put["current_value"] == -350.0


def test_a_changed_value_on_the_same_date_replaces_the_old_one(securo):
    state: dict = {}
    write(securo, state)
    changed = report()
    raw = next(p for p in changed.positions if p["conid"] == "100001")
    raw["positionValue"] = "690"
    write(securo, state, changed)
    exmp = asset(securo, EXMP)
    assert [v["amount"] for v in securo.values[exmp["id"]]] == [690.0]


def test_positions_gone_from_the_statement_are_sold_to_zero_and_archived(securo):
    state: dict = {}
    write(securo, state)
    later = replace(report(), positions=[p for p in report().positions if p["conid"] != "100001"])
    write(securo, state, later, start=date(2026, 11, 16), end=date(2026, 11, 16))
    exmp = asset(securo, EXMP)
    assert exmp["is_archived"] and exmp["units"] == 0
    assert securo.trades[exmp["id"]][-1]["notes"] == "IBKR position adjustment"
    assert securo.values[exmp["id"]][-1] == {**securo.values[exmp["id"]][-1], "amount": 0.0, "date": "2026-11-16"}
    assert state["assets"][EXMP]["open"] is False

    # Bought again later: the same asset comes back.
    write(securo, state, report(), start=date(2026, 11, 17), end=date(2026, 11, 17))
    again = asset(securo, EXMP)
    assert again["id"] == exmp["id"] and not again["is_archived"] and again["units"] == 3


# --- trades ----------------------------------------------------------------


def _with_trades() -> ParsedReport:
    rpt = report()
    base = {"conid": "100001", "currency": "USD", "assetCategory": "STK", "symbol": "EXMP", "multiplier": "1"}
    rpt.trades.extend([
        {**base, "tradeID": "T1", "tradeDate": "20260301", "quantity": "2", "tradePrice": "150",
         "buySell": "BUY", "proceeds": "-300", "netCash": "-301"},
        {**base, "tradeID": "T2", "tradeDate": "20260401", "quantity": "-1", "tradePrice": "180",
         "buySell": "SELL", "proceeds": "180", "netCash": "179", "fifoPnlRealized": "20"},
    ])
    gone = {"conid": "200001", "currency": "USD", "assetCategory": "STK", "symbol": "GONE",
            "description": "GONE INC", "multiplier": "1"}
    rpt.trades.extend([
        {**gone, "tradeID": "G1", "tradeDate": "20260110", "quantity": "-4", "tradePrice": "10",
         "buySell": "SELL", "proceeds": "40", "netCash": "39", "fifoPnlRealized": "7", "openCloseIndicator": "C"},
    ])
    return rpt


def test_ledger_units_equal_the_ibkr_position_with_an_opening_buy(securo):
    write(securo, {}, _with_trades())
    exmp = asset(securo, EXMP)
    ledger = securo.trades[exmp["id"]]
    assert [(t["kind"], t["quantity"], t["notes"]) for t in ledger] == [
        ("buy", 2.0, "IBKR opening position"),
        ("buy", 2.0, "IBKR trade T1"),
        ("sell", 1.0, "IBKR trade T2"),
    ]
    assert ledger[0]["date"] == "2025-01-02"  # the position's open date, before the window
    assert ledger[1]["fee"] == "1"
    assert exmp["units"] == 3


def test_instruments_closed_within_the_window_keep_their_trades_and_are_archived(securo):
    write(securo, {}, _with_trades())
    gone = asset(securo, "U1234567:default:200001")
    ledger = securo.trades[gone["id"]]
    assert [(t["kind"], t["quantity"], t["notes"]) for t in ledger] == [
        ("buy", 4.0, "IBKR opening position"),
        ("sell", 4.0, "IBKR trade G1"),
    ]
    # The opening is priced at the cost IBKR matched: (40 - 1 - 7) / 4.
    assert ledger[0]["price"] == 8.0 and ledger[0]["date"] == "2025-11-15"
    assert gone["is_archived"] and gone["units"] == 0


def test_short_positions_get_a_value_but_no_ledger(securo):
    summary = write(securo, {})
    put = asset(securo, PUT)
    assert securo.trades[put["id"]] == []
    assert summary.skipped_short == [PUT]


def test_trades_are_never_posted_twice_and_new_ones_append(securo):
    state: dict = {}
    write(securo, state, _with_trades())
    write(securo, state, _with_trades())
    exmp = asset(securo, EXMP)
    assert len(securo.trades[exmp["id"]]) == 3

    later = _with_trades()
    later = replace(later, trades=[{**later.trades[0], "tradeID": "T3", "tradeDate": "20261116"}])
    raw = next(p for p in later.positions if p["conid"] == "100001")
    raw["position"] = "5"
    write(securo, state, later, start=date(2026, 11, 16), end=date(2026, 11, 16))
    exmp = asset(securo, EXMP)
    assert [t["notes"] for t in securo.trades[exmp["id"]]][-1] == "IBKR trade T3"
    assert exmp["units"] == 5


def test_api_errors_carry_no_token_or_body(securo):
    api = SecuroApi(BASE, TOKEN, transport=lambda *a: (500, b'{"detail": "boom"}'))
    with pytest.raises(ApiError) as exc:
        api.get("/accounts")
    assert TOKEN not in str(exc.value) and exc.value.status == 500
