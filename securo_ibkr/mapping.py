"""Turn a `ParsedReport` into plain records: cash accounts, cash activity,
positions and trades.

Pure functions, no I/O and no Securo types. External-id formats match
Securo's built-in IBKR provider so its records can be adopted later (D7).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from securo_ibkr.flex import (
    ParsedReport,
    field_value,
    iso_currency,
    parse_date,
    parse_decimal,
    statement_amount,
)


@dataclass(frozen=True)
class CashAccount:
    external_id: str  # "{account_id}:{currency}"
    name: str
    balance: Decimal
    currency: str


@dataclass(frozen=True)
class CashTransaction:
    external_id: str  # "ibkr:{account_id}:{currency}:{transaction_id}"
    description: str
    amount: Decimal  # always positive; `type` carries the sign
    date: date
    type: str  # "credit" or "debit"
    currency: str
    payee: str
    raw_data: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Position:
    external_id: str  # "{account_id}:{model}:{conid}"
    name: str
    currency: str
    current_value: Decimal
    quantity: Decimal | None
    unit_price: Decimal | None
    purchase_price: Decimal | None
    purchase_date: date | None
    isin: str | None
    ticker: str | None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Trade:
    external_id: str  # "trade:{trade_id}"
    kind: str  # "buy" or "sell"
    date: date
    quantity: Decimal
    price: Decimal
    fee: Decimal


def account_label(report: ParsedReport) -> str:
    return report.account_alias or report.account_name or report.account_id


def cash_accounts(report: ParsedReport) -> list[CashAccount]:
    """One investment cash account per currency, sorted by currency."""
    label = account_label(report)
    return [
        CashAccount(
            external_id=f"{report.account_id}:{currency}",
            name=f"{label} Cash ({currency})",
            balance=balance,
            currency=currency,
        )
        for currency, balance in sorted(report.cash_balances.items())
    ]


def cash_transactions(
    report: ParsedReport, currency: str, since: date | None = None
) -> list[CashTransaction]:
    """Non-trade Statement of Funds rows in `currency`, on or after `since`."""
    currency = currency.upper()
    transactions: list[CashTransaction] = []
    seen: set[str] = set()
    for raw in report.statement_funds:
        if iso_currency(field_value(raw, "currency")) != currency:
            continue
        if field_value(raw, "tradeID", "tradeId", "tradeQuantity"):
            continue  # executions stay out of spending activity
        transaction_id = field_value(raw, "transactionID", "transactionId")
        if not transaction_id:
            continue  # starting/ending balance and aggregate rows
        txn_date = parse_date(
            field_value(raw, "date", "reportDate"),
            field="Statement of Funds / Date",
            required=True,
        )
        if txn_date is None or (since and txn_date < since):
            continue
        signed = statement_amount(raw)
        external_id = f"ibkr:{report.account_id}:{currency}:{transaction_id}"
        if external_id in seen:
            continue
        seen.add(external_id)
        description = (
            field_value(raw, "activityDescription", "description", "activityCode")
            or "IBKR cash activity"
        )[:500]
        symbol = field_value(raw, "symbol")
        transactions.append(
            CashTransaction(
                external_id=external_id[:255],
                description=description,
                amount=abs(signed),
                date=txn_date,
                type="credit" if signed >= 0 else "debit",
                currency=currency,
                payee=(symbol or "Interactive Brokers")[:255],
                raw_data=raw,
            )
        )
    return transactions


def positions(report: ParsedReport) -> list[Position]:
    """Open positions (Summary level), one per account/model/conid."""
    result: list[Position] = []
    for raw in report.positions:
        conid = field_value(raw, "conid")
        currency = iso_currency(field_value(raw, "currency"))
        position_value = parse_decimal(
            field_value(raw, "positionValue"),
            field="Open Positions / Position Value",
            required=True,
        )
        if not conid or not currency or position_value is None:
            raise ValueError(
                "IBKR Flex Open Positions must include Conid, Currency and Position Value"
            )
        model = field_value(raw, "model") or "default"
        quantity = parse_decimal(
            field_value(raw, "position", "quantity"),
            field="Open Positions / Position",
            required=True,
        )
        mark_price = parse_decimal(field_value(raw, "markPrice"), field="Open Positions / Mark Price")
        cost_basis = parse_decimal(
            field_value(raw, "costBasisMoney"), field="Open Positions / Cost Basis Money"
        )
        symbol = field_value(raw, "symbol")
        result.append(
            Position(
                external_id=f"{report.account_id}:{model}:{conid}",
                name=field_value(raw, "description", "symbol") or f"IBKR position {conid}",
                currency=currency,
                current_value=position_value,
                quantity=quantity,
                unit_price=mark_price,
                purchase_price=cost_basis,
                purchase_date=parse_date(
                    field_value(raw, "openDateTime"), field="Open Positions / Open Date"
                ),
                isin=field_value(raw, "isin"),
                ticker=symbol[:32] if symbol else None,
                metadata={
                    "account_id": report.account_id,
                    "model": None if model == "default" else model,
                    "conid": conid,
                    "asset_class": field_value(raw, "assetCategory", "assetClass"),
                    "side": field_value(raw, "side"),
                    "multiplier": field_value(raw, "multiplier"),
                    "mark_price": str(mark_price) if mark_price is not None else None,
                    "security_id": field_value(raw, "securityID", "securityId"),
                    "cusip": field_value(raw, "cusip"),
                    "figi": field_value(raw, "figi"),
                    "listing_exchange": field_value(raw, "listingExchange"),
                    "underlying_conid": field_value(raw, "underlyingConid"),
                    "underlying_symbol": field_value(raw, "underlyingSymbol"),
                    "strike": field_value(raw, "strike"),
                    "expiry": field_value(raw, "expiry"),
                    "put_call": field_value(raw, "putCall"),
                    "raw_data": raw,
                },
            )
        )
    return result


def trade_from_execution(row: dict[str, str]) -> tuple[str, Trade] | None:
    """Map a Trades/Trade execution row to (conid, trade)."""
    conid = field_value(row, "conid")
    trade_id = field_value(row, "tradeID", "tradeId", "transactionID", "transactionId")
    trade_date = parse_date(
        field_value(row, "tradeDate", "dateTime", "reportDate"), field="Trades / Trade Date"
    )
    quantity = parse_decimal(field_value(row, "quantity"), field="Trades / Quantity")
    price = parse_decimal(field_value(row, "tradePrice"), field="Trades / Trade Price")
    multiplier = parse_decimal(
        field_value(row, "multiplier"), field="Trades / Multiplier"
    ) or Decimal("1")
    side = (field_value(row, "buySell") or "").upper()
    if not conid or not trade_id or trade_date is None or not quantity or not price:
        return None
    kind = "sell" if side.startswith("SELL") or (not side and quantity < 0) else "buy"
    units = abs(quantity) * multiplier
    proceeds = parse_decimal(field_value(row, "proceeds"), field="Trades / Proceeds")
    net_cash = parse_decimal(field_value(row, "netCash"), field="Trades / Net Cash")
    if proceeds and net_cash is not None:
        # The cash IBKR actually settled, rounded to the cent: quantity *
        # tradePrice can differ from it by a fraction of a cent.
        price = abs(proceeds) / units
        fee = abs(proceeds - net_cash)
    else:
        currency = iso_currency(field_value(row, "currency"))
        fee = Decimal("0")
        commission = parse_decimal(field_value(row, "ibCommission"), field="Trades / Commission")
        if commission and iso_currency(field_value(row, "ibCommissionCurrency")) in {None, currency}:
            fee += abs(commission)
        taxes = parse_decimal(field_value(row, "taxes"), field="Trades / Taxes")
        if taxes:
            fee += abs(taxes)
        price = price * multiplier
    return conid, Trade(
        external_id=f"trade:{trade_id}",
        kind=kind,
        date=trade_date,
        quantity=units,
        price=price,
        fee=fee,
    )


def trade_from_statement_fund(row: dict[str, str]) -> tuple[str, Trade] | None:
    """Map a Statement of Funds trade row to (conid or symbol, trade).

    Only rows that carry the executed quantity and price qualify. Without a
    commission field, the fee is whatever cash moved beyond quantity * price.
    """
    trade_id = field_value(row, "tradeID", "tradeId")
    quantity = parse_decimal(
        field_value(row, "tradeQuantity"), field="Statement of Funds / Trade Quantity"
    )
    price = parse_decimal(field_value(row, "tradePrice"), field="Statement of Funds / Trade Price")
    key = field_value(row, "conid") or field_value(row, "symbol")
    trade_date = parse_date(
        field_value(row, "tradeDate", "date", "reportDate"), field="Statement of Funds / Date"
    )
    if not trade_id or not quantity or not price or not key or trade_date is None:
        return None
    code = (field_value(row, "activityCode") or "").upper()
    kind = "sell" if code.startswith("SELL") or (not code and quantity < 0) else "buy"
    quantity = abs(quantity)
    gross = quantity * price
    amount = statement_amount(row)
    commission = parse_decimal(
        field_value(row, "tradeCommission"), field="Statement of Funds / Commission"
    )
    if commission is not None:
        fee = abs(commission)
    else:
        fee = (-amount - gross) if kind == "buy" else (gross - amount)
    return key, Trade(
        external_id=f"trade:{trade_id}",
        kind=kind,
        date=trade_date,
        quantity=quantity,
        price=price,
        fee=max(fee, Decimal("0")),
    )


def trades_for(report: ParsedReport, positions: list[Position]) -> dict[str, list[Trade]]:
    """Executions in `report` for `positions`, keyed by position external_id.

    Prefers the Trades section, falling back to Statement of Funds trade rows.
    Every position gets an entry; an empty list means no trades in the report.
    """
    by_key: dict[str, Position | None] = {}
    for position in positions:
        for key in (position.metadata.get("conid"), position.ticker):
            if key:
                # Ambiguous keys (same symbol in two models) map nowhere.
                by_key[key] = position if key not in by_key else None

    if report.trades:
        parsed = [(row, trade_from_execution(row)) for row in report.trades]
    else:
        parsed = [
            (row, trade_from_statement_fund(row))
            for row in report.statement_funds
            if field_value(row, "tradeID", "tradeId")
        ]
    trades: dict[str, list[Trade]] = {p.external_id: [] for p in positions}
    seen: set[tuple[str, str]] = set()
    for row, item in parsed:
        if item is None:
            continue
        key, trade = item
        position = by_key.get(key)
        # A row in another currency segment is a converted copy.
        if position is None or iso_currency(field_value(row, "currency")) != position.currency:
            continue
        # Statement of Funds repeats a trade per currency segment.
        identity = (position.external_id, trade.external_id)
        if identity in seen:
            continue
        seen.add(identity)
        trades[position.external_id].append(trade)
    return trades
