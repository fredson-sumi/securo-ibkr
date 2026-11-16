"""The ledger planner (D12). Pure: no API."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from securo_ibkr.ledger import ADJUSTMENT_NOTE, OPENING_NOTE, Entry, plan
from securo_ibkr.mapping import Trade

D = Decimal
KW = dict(opening_date=date(2025, 11, 15), opening_price=D("100"), adjust_date=date(2026, 11, 15), adjust_price=D("120"))


def trade(tid: str, kind: str, qty: str, day: date) -> Trade:
    return Trade(f"trade:{tid}", kind, day, D(qty), D("110"), D("1"))


def held(entries: list[Entry]) -> Decimal:
    return sum((e.signed for e in entries), D("0"))


def test_empty_ledger_gets_an_opening_buy_for_quantity_held_before_the_window():
    trades = [trade("A", "buy", "2", date(2026, 3, 1)), trade("B", "sell", "1", date(2026, 4, 1))]
    posts = plan([], trades, D("5"), **KW)
    assert [(p.kind, p.quantity, p.notes) for p in posts] == [
        ("buy", D("4"), OPENING_NOTE), ("buy", D("2"), "IBKR trade A"), ("sell", D("1"), "IBKR trade B"),
    ]
    assert posts[0].date == date(2025, 11, 15) and posts[0].price == D("100")


def test_opening_covers_sells_even_when_the_position_is_now_smaller():
    # Sold 6, bought 2 back, holds 1 now: the opening must cover the sell.
    trades = [trade("S", "sell", "6", date(2026, 1, 5)), trade("B", "buy", "2", date(2026, 2, 1))]
    posts = plan([], trades, D("1"), **KW)
    assert posts[0] == Entry("buy", D("6"), date(2025, 11, 15), D("100"), notes=OPENING_NOTE)
    assert posts[-1].notes == ADJUSTMENT_NOTE and posts[-1].kind == "sell" and posts[-1].quantity == D("1")
    assert held(posts) == D("1")


def test_known_trades_are_skipped_and_a_split_is_one_adjustment():
    existing = [Entry("buy", D("10"), date(2025, 11, 15), notes=OPENING_NOTE),
                Entry("buy", D("2"), date(2026, 3, 1), notes="IBKR trade A")]
    posts = plan(existing, [trade("A", "buy", "2", date(2026, 3, 1))], D("24"), **KW)
    assert posts == [Entry("buy", D("12"), date(2026, 11, 15), D("120"), notes=ADJUSTMENT_NOTE)]


def test_a_later_sell_the_ledger_cannot_cover_gets_a_buy_just_before_it():
    existing = [Entry("buy", D("1"), date(2025, 11, 15), notes=OPENING_NOTE)]
    posts = plan(existing, [trade("S", "sell", "3", date(2026, 5, 2))], D("0"), **KW)
    assert [(p.kind, p.quantity, p.date) for p in posts] == [
        ("buy", D("2"), date(2026, 5, 1)), ("sell", D("3"), date(2026, 5, 2)),
    ]


def test_nothing_to_post_when_the_ledger_already_matches():
    existing = [Entry("buy", D("3"), date(2025, 1, 2), notes=OPENING_NOTE)]
    assert plan(existing, [], D("3.0000001"), **KW) == []
