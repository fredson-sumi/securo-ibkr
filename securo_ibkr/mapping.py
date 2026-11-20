"""Turn a `ParsedReport` into plain records: cash accounts, cash activity,
positions and trades.

Pure functions, no I/O and no Securo types. Securo matches records by their
external id, so the formats here never change (D7).
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
    price: Decimal  # per unit; units are contracts x multiplier
    fee: Decimal
    #: IBKR's FIFO realized P/L on a closing execution, when the query has it.
    realized: Decimal | None = None
    #: A sell that opens (or adds to) a short position.
    opens_short: bool = False


@dataclass
class Holding:
    """One instrument: an open position, or one traded and closed in the window."""

    external_id: str  # "{account_id}:{model}:{conid}"
    conid: str
    name: str
    type: str  # Securo asset type
    currency: str
    ticker: str | None
    multiplier: Decimal
    position: Position | None  # None: closed by the end of the report
    trades: list[Trade] = field(default_factory=list)

    @property
    def is_short(self) -> bool:
        if self.position is not None and self.position.quantity is not None:
            return self.position.quantity < 0
        return any(t.opens_short for t in self.trades)

    @property
    def units(self) -> Decimal:
        """Units held at the end of the statement: contracts x multiplier, 0 when closed."""
        if self.position is None or self.position.quantity is None:
            return Decimal("0")
        return self.position.quantity * self.multiplier


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
            field="Open Positions / Quantity",
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
                    field_value(raw, "openDateTime"), field="Open Positions / Open Date Time"
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


_ASSET_TYPES = {"STK": "stock", "FUND": "fund", "CRYPTO": "crypto"}


def asset_type(row: dict[str, str]) -> str:
    category = (field_value(row, "assetCategory", "assetClass") or "").upper()
    if category == "STK" and (field_value(row, "subCategory") or "").upper() == "ETF":
        return "etf"
    return _ASSET_TYPES.get(category, "investment")


def trade_from_execution(row: dict[str, str]) -> tuple[str, Trade] | None:
    """Map a Trades/Trade execution row to (conid, trade)."""
    conid = field_value(row, "conid")
    trade_id = field_value(row, "tradeID", "tradeId", "transactionID", "transactionId")
    trade_date = parse_date(
        field_value(row, "tradeDate", "dateTime", "reportDate"), field="Trades / Trade Date"
    )
    quantity = parse_decimal(field_value(row, "quantity"), field="Trades / Quantity")
    price = parse_decimal(field_value(row, "tradePrice"), field="Trades / TradePrice")
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
        commission = parse_decimal(field_value(row, "ibCommission"), field="Trades / IB Commission")
        if commission and iso_currency(field_value(row, "ibCommissionCurrency")) in {None, currency}:
            fee += abs(commission)
        taxes = parse_decimal(field_value(row, "taxes"), field="Trades / Taxes")
        if taxes:
            fee += abs(taxes)
        # tradePrice is already per unit (per share, not per contract).
    open_close = (field_value(row, "openCloseIndicator") or "").upper()
    return conid, Trade(
        external_id=f"trade:{trade_id}",
        kind=kind,
        date=trade_date,
        quantity=units,
        price=abs(price),
        fee=fee,
        realized=parse_decimal(
            field_value(row, "fifoPnlRealized"), field="Trades / Realized P/L"
        ),
        opens_short=kind == "sell" and "O" in open_close.split(";"),
    )


def trade_from_statement_fund(
    row: dict[str, str], multiplier: Decimal = Decimal("1")
) -> tuple[str, Trade] | None:
    """Map a Statement of Funds trade row to (conid or symbol, trade).

    Only rows that carry the executed quantity and price qualify. Without a
    commission field, the fee is whatever cash moved beyond units * price.
    Statement of Funds has no multiplier, so the caller passes the position's
    (D13): without it an option's gross is 1/100 of the cash that moved.
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
    units = abs(quantity) * multiplier
    price = abs(price)
    gross = units * price
    amount = statement_amount(row)
    commission = parse_decimal(
        field_value(row, "tradeCommission"), field="Statement of Funds / Trade Commission"
    )
    if commission is not None:
        fee = abs(commission)
    else:
        fee = (-amount - gross) if kind == "buy" else (gross - amount)
    return key, Trade(
        external_id=f"trade:{trade_id}",
        kind=kind,
        date=trade_date,
        quantity=units,
        price=price,
        fee=max(fee, Decimal("0")),
    )


def _multiplier(row: dict[str, str]) -> Decimal:
    value = parse_decimal(field_value(row, "multiplier"), field="Multiplier")
    return value if value and value > 0 else Decimal("1")


def holdings(report: ParsedReport) -> list[Holding]:
    """Every instrument worth an asset, with its executions in the report.

    Open positions come first. With a Trades section, instruments that were
    traded but are no longer held also become holdings (closed), so their
    realized gains land in the ledger. Cash conversions (asset class CASH)
    are not holdings. Statement of Funds trade rows only attach to open
    positions: they can't describe an instrument on their own.
    """
    result: list[Holding] = []
    for position in positions(report):
        raw = position.metadata.get("raw_data") or {}
        result.append(
            Holding(
                external_id=position.external_id,
                conid=position.metadata["conid"],
                name=position.name,
                type=asset_type(raw),
                currency=position.currency,
                ticker=position.ticker,
                multiplier=_multiplier(raw),
                position=position,
            )
        )
    by_conid = {h.conid: h for h in result}
    by_ticker: dict[str, Holding | None] = {}
    for holding in result:
        if holding.ticker:
            # Ambiguous tickers (same symbol in two models) map nowhere.
            by_ticker[holding.ticker] = holding if holding.ticker not in by_ticker else None
    seen: set[tuple[str, str]] = set()

    def attach(holding: Holding | None, row: dict[str, str], trade: Trade) -> None:
        # A row in another currency segment is a converted copy.
        if holding is None or iso_currency(field_value(row, "currency")) != holding.currency:
            return
        identity = (holding.external_id, trade.external_id)
        if identity in seen:  # Statement of Funds repeats a trade per segment
            return
        seen.add(identity)
        holding.trades.append(trade)

    if report.trades:
        for row in report.trades:
            item = trade_from_execution(row)
            if item is None:
                continue
            conid, trade = item
            holding = by_conid.get(conid)
            if holding is None:
                category = (field_value(row, "assetCategory", "assetClass") or "").upper()
                currency = iso_currency(field_value(row, "currency"))
                if category == "CASH" or not currency:
                    continue
                model = field_value(row, "model") or "default"
                symbol = field_value(row, "symbol")
                holding = Holding(
                    external_id=f"{report.account_id}:{model}:{conid}",
                    conid=conid,
                    name=field_value(row, "description", "symbol") or f"IBKR position {conid}",
                    type=asset_type(row),
                    currency=currency,
                    ticker=symbol[:32] if symbol else None,
                    multiplier=_multiplier(row),
                    position=None,
                )
                by_conid[conid] = holding
                result.append(holding)
            attach(holding, row, trade)
    else:
        for row in report.statement_funds:
            if not field_value(row, "tradeID", "tradeId"):
                continue
            key = field_value(row, "conid") or field_value(row, "symbol")
            holding = by_conid.get(key or "") or by_ticker.get(key or "")
            if holding is None:
                continue
            item = trade_from_statement_fund(row, holding.multiplier)
            if item is not None:
                attach(holding, row, item[1])
    for holding in result:
        holding.trades.sort(key=lambda t: t.date)
    return result
