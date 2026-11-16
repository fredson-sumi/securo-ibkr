"""Write a parsed statement into Securo as manual records.

Every step is idempotent, so a sync cut short (time budget, crash, lost
response) is finished by the next one without duplicates:

- cash accounts: mapped in `state["accounts"]`, adopted by name (F1)
- cash activity: deduped on `external_id` against what the account holds (F3)
- assets: created by `external_id`, which returns the existing one (F4)
- trades: deduped on the ledger note `IBKR trade <id>` (F7, D12)
- values: one per statement end date, replaced when it changed (F5, D11)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from securo_ibkr import ledger
from securo_ibkr.api import NotFound, SecuroApi
from securo_ibkr.flex import ParsedReport
from securo_ibkr.mapping import CashAccount, CashTransaction, Holding, cash_accounts, cash_transactions, holdings

CENT = Decimal("0.01")
PAGE = 500


def _dec(value: Any) -> Decimal:
    return Decimal(str(value if value is not None else 0))


@dataclass
class Summary:
    accounts_created: int = 0
    transactions_created: int = 0
    assets: int = 0
    trades_posted: int = 0
    archived: int = 0
    values_written: int = 0
    skipped_short: list[str] = field(default_factory=list)

    def to_state(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v}


# --- cash accounts and activity ---------------------------------------------


def _find_account(api: SecuroApi, cash: CashAccount) -> dict | None:
    for account in api.get("/accounts"):
        if account.get("name") == cash.name and account.get("currency") == cash.currency:
            return account
    return None


def ensure_account(
    api: SecuroApi, cash: CashAccount, entry: dict | None, opening_date: date, summary: Summary
) -> dict:
    if entry:
        try:
            return api.get(f"/accounts/{entry['id']}")
        except NotFound:
            pass  # deleted in Securo: adopt by name or create again
    account = _find_account(api, cash)
    if account is None:
        account = api.post(
            "/accounts",
            {
                "name": cash.name,
                "type": "investment",
                "balance": str(cash.balance),
                "balance_date": opening_date.isoformat(),
                "currency": cash.currency,
            },
        )
        summary.accounts_created += 1
    return account


def _known_external_ids(api: SecuroApi, account_id: str, start: date, end: date) -> set[str]:
    known: set[str] = set()
    page = 1
    while True:
        result = api.get(
            "/transactions", account_id=account_id, **{"from": start.isoformat(), "to": end.isoformat()},
            limit=PAGE, page=page,
        )
        items = result.get("items") or []
        known.update(t["external_id"] for t in items if t.get("external_id"))
        if len(items) < PAGE or page * PAGE >= int(result.get("total") or 0):
            return known
        page += 1


def write_transactions(
    api: SecuroApi, account_id: str, transactions: list[CashTransaction], summary: Summary
) -> None:
    if not transactions:
        return
    dates = [t.date for t in transactions]
    known = _known_external_ids(api, account_id, min(dates), max(dates))
    for tx in sorted(transactions, key=lambda t: (t.date, t.external_id)):
        if tx.external_id in known:
            continue
        api.post(
            "/transactions",
            {
                "account_id": account_id,
                "description": tx.description,
                "amount": str(tx.amount),
                "date": tx.date.isoformat(),
                "type": tx.type,
                "currency": tx.currency,
                "external_id": tx.external_id,
                "status": "posted",
            },
        )
        known.add(tx.external_id)
        summary.transactions_created += 1


def reconcile_balance(api: SecuroApi, account_id: str, ending_cash: Decimal, opening_date: date) -> None:
    """Set the opening so the current balance equals IBKR's ending cash (D10)."""
    account = api.get(f"/accounts/{account_id}")
    activity = _dec(account.get("current_balance")) - _dec(account.get("balance"))
    opening = (ending_cash - activity).quantize(CENT)
    if _dec(account.get("balance")).quantize(CENT) == opening and account.get("balance_date") == opening_date.isoformat():
        return
    api.patch(f"/accounts/{account_id}", {"balance": str(opening), "balance_date": opening_date.isoformat()})


def write_cash(api: SecuroApi, report: ParsedReport, state: dict, start: date, summary: Summary) -> None:
    mapped = state.setdefault("accounts", {})
    for cash in cash_accounts(report):
        entry = mapped.get(cash.external_id) or {}
        opening_date = start - timedelta(days=1)
        if entry.get("opening_date"):
            opening_date = min(opening_date, date.fromisoformat(entry["opening_date"]))
        account = ensure_account(api, cash, entry, opening_date, summary)
        mapped[cash.external_id] = {"id": account["id"], "opening_date": opening_date.isoformat()}
        write_transactions(api, account["id"], cash_transactions(report, cash.currency, since=start), summary)
        reconcile_balance(api, account["id"], cash.balance, opening_date)


# --- assets, values and trades -----------------------------------------------


def ensure_asset(api: SecuroApi, holding: Holding, entry: dict | None) -> dict:
    asset = None
    if entry:
        try:
            asset = api.get(f"/assets/{entry['id']}")
        except NotFound:
            pass  # deleted in Securo: create it again
    if asset is None:
        asset = _create_asset(api, holding)
    if asset.get("is_archived") and holding.position is not None:
        asset = api.patch(f"/assets/{asset['id']}", {"is_archived": False})
    return asset


def _create_asset(api: SecuroApi, holding: Holding) -> dict:
    body: dict[str, Any] = {
        "name": holding.name[:255],
        "type": holding.type,
        "currency": holding.currency,
        "valuation_method": "manual",
        "external_id": holding.external_id,
    }
    if holding.type in {"stock", "etf", "fund"} and holding.ticker:
        body["ticker"] = holding.ticker  # manual valuation: never quoted live
    return api.post("/assets", body)  # returns the existing asset for this external_id (F4)


def write_value(api: SecuroApi, asset_id: str, amount: Decimal, day: date, summary: Summary) -> None:
    """One value per statement end date (D11)."""
    same_day = [v for v in api.get(f"/assets/{asset_id}/values") if str(v.get("date")) == day.isoformat()]
    if len(same_day) == 1 and _dec(same_day[0]["amount"]).quantize(CENT) == amount.quantize(CENT):
        return
    for value in same_day:
        api.delete(f"/assets/values/{value['id']}")
    api.post(f"/assets/{asset_id}/values", {"amount": str(amount.quantize(CENT)), "date": day.isoformat()})
    summary.values_written += 1


def _prices(holding: Holding) -> tuple[Decimal, Decimal]:
    """(opening price, adjustment price) per unit."""
    last_trade = holding.trades[-1].price if holding.trades else Decimal("0")
    position = holding.position
    if position is not None:
        mark = position.unit_price if position.unit_price is not None else last_trade
        units = holding.units
        if position.purchase_price and units > 0:
            return abs(position.purchase_price) / units, abs(mark)
        return abs(mark), abs(mark)
    # Closed: the cost IBKR matched against the first sell with a realized P/L.
    for trade in holding.trades:
        if trade.kind == "sell" and trade.realized is not None and trade.quantity:
            cost = trade.price * trade.quantity - trade.fee - trade.realized
            return max(cost / trade.quantity, Decimal("0")), last_trade
    first = holding.trades[0].price if holding.trades else Decimal("0")
    return first, last_trade


def write_trades(
    api: SecuroApi, asset_id: str, holding: Holding, start: date, end: date, summary: Summary
) -> None:
    existing = [ledger.from_api(row) for row in api.get(f"/assets/{asset_id}/transactions")]
    opening_price, adjust_price = _prices(holding)
    opening_date = start - timedelta(days=1)
    if holding.position is not None and holding.position.purchase_date:
        opening_date = min(opening_date, holding.position.purchase_date)
    for entry in ledger.plan(
        existing, holding.trades, holding.units,
        opening_date=opening_date, opening_price=opening_price,
        adjust_date=end, adjust_price=adjust_price,
    ):
        api.post(f"/assets/{asset_id}/transactions", entry.payload())
        summary.trades_posted += 1


def _close(api: SecuroApi, asset: dict, end: date, summary: Summary) -> None:
    write_value(api, asset["id"], Decimal("0"), end, summary)
    if not asset.get("is_archived"):
        api.patch(f"/assets/{asset['id']}", {"is_archived": True})
        summary.archived += 1


def write_holdings(
    api: SecuroApi, report: ParsedReport, state: dict, start: date, end: date, summary: Summary
) -> None:
    mapped = state.setdefault("assets", {})
    seen: set[str] = set()
    for holding in holdings(report):
        seen.add(holding.external_id)
        asset = ensure_asset(api, holding, mapped.get(holding.external_id))
        mapped[holding.external_id] = {"id": asset["id"], "open": holding.position is not None}
        summary.assets += 1
        if holding.is_short:
            # The ledger can't hold a short position (F6): value only.
            summary.skipped_short.append(holding.external_id)
        else:
            write_trades(api, asset["id"], holding, start, end, summary)
        if holding.position is not None:
            write_value(api, asset["id"], holding.position.current_value, end, summary)
        else:
            _close(api, asset, end, summary)

    # Held before, absent now, and not traded in this statement: closed by
    # something outside the Trades section (expiry, assignment, transfer).
    for external_id, entry in mapped.items():
        if external_id in seen or not entry.get("open"):
            continue
        try:
            asset = api.get(f"/assets/{entry['id']}")
        except NotFound:
            entry["open"] = False
            continue
        existing = [ledger.from_api(row) for row in api.get(f"/assets/{entry['id']}/transactions")]
        price = _dec(asset.get("average_price"))
        for post in ledger.plan(
            existing, [], Decimal("0"),
            opening_date=end, opening_price=price, adjust_date=end, adjust_price=price,
        ) if existing else []:
            api.post(f"/assets/{entry['id']}/transactions", post.payload())
            summary.trades_posted += 1
        _close(api, asset, end, summary)
        entry["open"] = False


def write_report(api: SecuroApi, report: ParsedReport, state: dict, start: date, end: date) -> Summary:
    summary = Summary()
    write_cash(api, report, state, start, summary)
    write_holdings(api, report, state, start, end, summary)
    return summary
