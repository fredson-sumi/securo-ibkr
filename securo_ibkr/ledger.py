"""Plan the buys and sells that make an asset's ledger match IBKR (D12).

Securo's asset ledger owns `units` and rejects a sell that the units held at
that date can't cover (F6). Pure functions: given what the ledger already
holds and what IBKR reports, return the entries to post, in order.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable

from securo_ibkr.mapping import Trade

OPENING_NOTE = "IBKR opening position"
ADJUSTMENT_NOTE = "IBKR position adjustment"
#: The ledger stores quantities with 6 decimals.
TOLERANCE = Decimal("0.000001")


@dataclass(frozen=True)
class Entry:
    """A buy or sell, either already in the ledger or to be posted."""

    kind: str
    quantity: Decimal
    date: date
    price: Decimal = Decimal("0")
    fee: Decimal = Decimal("0")
    notes: str | None = None

    @property
    def signed(self) -> Decimal:
        return self.quantity if self.kind == "buy" else -self.quantity

    def payload(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "quantity": str(self.quantity),
            "price": str(self.price),
            "fee": str(self.fee),
            "date": self.date.isoformat(),
            "notes": self.notes or "",
        }


def from_api(row: dict) -> Entry:
    return Entry(
        kind=row["kind"],
        quantity=Decimal(str(row["quantity"])),
        date=date.fromisoformat(str(row["date"])),
        price=Decimal(str(row.get("price") or 0)),
        notes=row.get("notes"),
    )


def from_trade(trade: Trade) -> Entry:
    return Entry(trade.kind, trade.quantity, trade.date, trade.price, trade.fee, trade.notes)


def _lowest_running(entries: Iterable[Entry], start: Decimal = Decimal("0")) -> Decimal:
    running = low = start
    for entry in sorted(entries, key=lambda e: e.date):
        running += entry.signed
        low = min(low, running)
    return low


def plan(
    existing: list[Entry],
    trades: list[Trade],
    target: Decimal,
    *,
    opening_date: date,
    opening_price: Decimal,
    adjust_date: date,
    adjust_price: Decimal,
) -> list[Entry]:
    """Entries to post so the ledger holds `target` units after `trades`.

    - Executions already in the ledger (same note) are skipped.
    - An empty ledger first gets an opening buy for the quantity held before
      the report, dated `opening_date` or the day before the first trade.
    - A later report whose sells the ledger can't cover gets an adjustment buy
      the day before its first new trade.
    - Whatever still differs from `target` (splits, transfers, corporate
      actions, history older than the report) is one adjustment on
      `adjust_date` at `adjust_price`.
    """
    known = {e.notes for e in existing if e.notes}
    new = [from_trade(t) for t in sorted(trades, key=lambda t: t.date) if t.notes not in known]
    posts: list[Entry] = []
    if not existing:
        net = sum((e.signed for e in new), Decimal("0"))
        # Enough to end at `target`, and to cover every sell along the way.
        opening = max(target - net, -_lowest_running(new))
        if opening > TOLERANCE:
            first = min([opening_date] + [e.date - timedelta(days=1) for e in new])
            posts.append(Entry("buy", opening, first, opening_price, notes=OPENING_NOTE))
    else:
        deficit = -_lowest_running(existing + new)
        if deficit > TOLERANCE and new:
            when = new[0].date - timedelta(days=1)
            posts.append(Entry("buy", deficit, when, adjust_price, notes=ADJUSTMENT_NOTE))
    posts.extend(new)
    held = sum((e.signed for e in existing + posts), Decimal("0"))
    diff = target - held
    if abs(diff) > TOLERANCE:
        kind = "buy" if diff > 0 else "sell"
        posts.append(Entry(kind, abs(diff), adjust_date, adjust_price, notes=ADJUSTMENT_NOTE))
    return posts
