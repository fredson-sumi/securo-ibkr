"""Build what `sync` returns to Securo: records in the provider plugin contract (1).

Securo writes these through its bank sync pipeline, the same code its native
connections use: it matches every record by `external_id`, so sending a day
twice changes nothing. The external-id formats are the ones Securo's former
built-in IBKR provider used (D7).

- One investment account per cash currency, its balance IBKR's ending cash,
  with the currency's cash activity (deposits, dividends, interest, fees).
- One holding per instrument: open positions with their value, and
  instruments traded and closed within the statement with quantity 0, so
  their realized gains are kept (D16).
- Each holding's executions as trades. When the first statement doesn't
  explain the whole position (it was bought before the statement starts),
  an opening buy is added the day before, priced at IBKR's cost (D21).
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from securo_ibkr.flex import ParsedReport
from securo_ibkr.mapping import Holding, Trade, cash_accounts, cash_transactions, holdings

CONTRACT = 1
#: The same favicon service Securo uses for its native connections' logos.
INSTITUTION_LOGO = "https://www.google.com/s2/favicons?domain=interactivebrokers.com&sz=128"
#: The contract takes at most 8 decimals; Securo stores 6.
_PLACES = Decimal("0.00000001")


def number(value: Decimal) -> str:
    """A decimal as the contract wants it: a plain string, no exponent, at most 8 decimals."""
    return format(value.quantize(_PLACES).normalize(), "f")


def opening_id(holding: Holding) -> str:
    return f"opening:{holding.external_id}"


def build(report: ParsedReport, start: date, end: date, state: dict[str, Any]) -> dict[str, Any]:
    """The contract payload for one statement. Updates `state["openings"]`."""
    first_statement = not state.get("last_report_end")
    openings: dict[str, Any] = state.setdefault("openings", {})
    return {
        "contract": CONTRACT,
        "accounts": [_account(report, account.currency, account.external_id, account.name, account.balance)
                     for account in cash_accounts(report)],
        "holdings": [
            _holding(holding, start, openings, first_statement) for holding in holdings(report)
        ],
    }


def _account(report: ParsedReport, currency: str, external_id: str, name: str, balance: Decimal) -> dict[str, Any]:
    return {
        "external_id": external_id,
        "name": name,
        "type": "investment",
        "balance": number(balance),
        "currency": currency,
        "masked_number": report.account_id[-4:],
        # No institution name: Securo would show it in place of each account's
        # name, and every currency's account would read "Interactive Brokers".
        # The logo alone becomes the connection's.
        "institution_logo_url": INSTITUTION_LOGO,
        "transactions": [
            {
                "external_id": t.external_id,
                "description": t.description,
                "amount": number(t.amount),
                "date": t.date.isoformat(),
                "type": t.type,
                "currency": t.currency,
                "payee": t.payee[:200],
                "raw_data": t.raw_data,
            }
            for t in cash_transactions(report, currency)
        ],
    }


def _holding(holding: Holding, start: date, openings: dict[str, Any], first_statement: bool) -> dict[str, Any]:
    position = holding.position
    units = holding.units
    record: dict[str, Any] = {
        "external_id": holding.external_id,
        "name": holding.name[:255],
        "type": holding.type,  # stock, etf, fund, crypto or investment (D24)
        "currency": holding.currency,
        "current_value": number(position.current_value if position is not None else Decimal("0")),
        "quantity": number(units),
        "ticker": holding.ticker,
        "metadata": _metadata(holding),
    }
    if position is not None:
        if units and position.current_value is not None:
            record["unit_price"] = number(abs(position.current_value / units))
        if position.purchase_price is not None:
            record["purchase_price"] = number(abs(position.purchase_price))
        if position.purchase_date is not None:
            record["purchase_date"] = position.purchase_date.isoformat()
        if position.isin and len(position.isin) == 12:
            record["isin"] = position.isin.upper()
    if holding.is_short:
        # Securo's ledger holds no short positions (D17): the value only.
        record["trades"] = None
        return record
    trades = [_trade(t) for t in holding.trades]
    opening = openings.get(holding.external_id)
    if opening is None and first_statement:
        opening = _opening(holding, start)
        if opening is not None:
            openings[holding.external_id] = opening
    if opening is not None:
        trades.insert(0, opening)
    record["trades"] = trades
    return record


def _trade(trade: Trade) -> dict[str, Any]:
    return {
        "external_id": trade.external_id,
        "kind": trade.kind,
        "date": trade.date.isoformat(),
        "quantity": number(trade.quantity),
        "price": number(trade.price),
        "fee": number(trade.fee),
    }


def _opening(holding: Holding, start: date) -> dict[str, Any] | None:
    """The buy that explains what was held before the first statement, if anything was.

    Enough units to end at the reported position and to cover every sell
    along the way, dated the day before the statement (or before the first
    trade). Only the first statement gets one: later statements continue the
    trades Securo already keeps.
    """
    net = Decimal("0")
    lowest = Decimal("0")
    for trade in sorted(holding.trades, key=lambda t: (t.date, t.kind != "buy")):
        net += trade.quantity if trade.kind == "buy" else -trade.quantity
        lowest = min(lowest, net)
    quantity = max(holding.units - net, -lowest)
    if quantity <= 0:
        return None
    day = start - timedelta(days=1)
    if holding.trades:
        day = min(day, holding.trades[0].date - timedelta(days=1))
    return {
        "external_id": opening_id(holding),
        "kind": "buy",
        "date": day.isoformat(),
        "quantity": number(quantity),
        "price": number(_opening_price(holding)),
        "fee": "0",
    }


def _opening_price(holding: Holding) -> Decimal:
    """Per unit: IBKR's cost basis for an open position, the cost it matched for a closed one."""
    position = holding.position
    if position is not None:
        units = holding.units
        if position.purchase_price and units > 0:
            return abs(position.purchase_price) / units
        if position.unit_price is not None:
            return abs(position.unit_price)
    for trade in holding.trades:
        if trade.kind == "sell" and trade.realized is not None and trade.quantity:
            cost = trade.price * trade.quantity - trade.fee - trade.realized
            return max(cost / trade.quantity, Decimal("0"))
    return holding.trades[0].price if holding.trades else Decimal("0")


def _metadata(holding: Holding) -> dict[str, Any]:
    if holding.position is not None:
        return {k: v for k, v in holding.position.metadata.items() if k != "raw_data" and v is not None}
    return {"conid": holding.conid, "multiplier": str(holding.multiplier)}
