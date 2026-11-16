"""An in-memory Securo API with the behaviours the spike found (F1-F8).

Used as a `SecuroApi` transport: it gets (method, url, headers, body) and
answers (status, json bytes), so the client is exercised too.
"""
from __future__ import annotations

import itertools
import json
import urllib.parse
from datetime import date
from decimal import Decimal
from typing import Any

BASE = "http://backend:8000/api"
TOKEN = "fake-api-token"


def _d(value: Any) -> Decimal:
    return Decimal(str(value))


class FakeSecuro:
    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self.accounts: dict[str, dict] = {}
        self.transactions: list[dict] = []
        self.assets: dict[str, dict] = {}
        self.values: dict[str, list[dict]] = {}
        self.trades: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, str]] = []

    def _id(self) -> str:
        return f"id-{next(self._ids)}"

    # --- transport ---------------------------------------------------------------

    def __call__(self, method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, bytes]:
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        parts = urllib.parse.urlsplit(url)
        path = parts.path.removeprefix("/api")
        query = dict(urllib.parse.parse_qsl(parts.query))
        data = json.loads(body) if body else {}
        self.calls.append((method, path))
        try:
            status, result = self.route(method, path.strip("/").split("/"), query, data)
        except LookupError:
            return 404, b'{"detail": "Not found"}'
        return status, json.dumps(result, default=str).encode() if result is not None else b""

    def writes(self) -> list[tuple[str, str]]:
        return [c for c in self.calls if c[0] != "GET"]

    def route(self, method: str, seg: list[str], query: dict, data: dict) -> tuple[int, Any]:
        match method, seg:
            case "GET", ["accounts"]:
                return 200, [self.account_read(a) for a in self.accounts.values()]
            case "GET", ["accounts", aid]:
                return 200, self.account_read(self.accounts[aid])
            case "POST", ["accounts"]:
                return 201, self.create_account(data)
            case "PATCH", ["accounts", aid]:
                return 200, self.patch_account(self.accounts[aid], data)
            case "GET", ["transactions"]:
                return 200, self.list_transactions(query)
            case "POST", ["transactions"]:
                tx = {"id": self._id(), "source": "manual", **data}  # F3: no dedup
                self.transactions.append(tx)
                return 201, tx
            case "POST", ["assets"]:
                return 201, self.create_asset(data)
            case "GET", ["assets", aid]:
                return 200, self.asset_read(self.assets[aid])
            case "PATCH", ["assets", aid]:
                asset = self.assets[aid]
                asset.update({k: v for k, v in data.items() if k != "units"})  # F6
                return 200, self.asset_read(asset)
            case "GET", ["assets", aid, "values"]:
                self.assets[aid]
                return 200, self.values[aid]
            case "POST", ["assets", aid, "values"]:
                self.assets[aid]
                value = {"id": self._id(), "asset_id": aid, "amount": float(data["amount"]), "date": data["date"], "source": "manual"}
                self.values[aid].append(value)  # F5: a second row on the same date
                return 201, value
            case "DELETE", ["assets", "values", vid]:
                for rows in self.values.values():
                    for row in rows:
                        if row["id"] == vid:
                            rows.remove(row)
                            return 204, None
                raise LookupError
            case "GET", ["assets", aid, "transactions"]:
                self.assets[aid]
                return 200, self.trades[aid]
            case "POST", ["assets", aid, "transactions"]:
                return self.add_trade(self.assets[aid], data)
        raise AssertionError(f"unexpected call {method} /{'/'.join(seg)}")

    # --- accounts (F1, F2) -------------------------------------------------------

    def create_account(self, data: dict) -> dict:
        account = {
            "id": self._id(), "name": data["name"], "type": data["type"], "currency": data["currency"],
            "balance": _d(data.get("balance", "0")), "balance_date": data.get("balance_date"),
            "external_id": None,  # F1: ignored
        }
        self.accounts[account["id"]] = account
        return self.account_read(account)

    def patch_account(self, account: dict, data: dict) -> dict:
        if "balance" in data:
            account["balance"] = _d(data["balance"])
        if "balance_date" in data:
            account["balance_date"] = data["balance_date"]
        return self.account_read(account)

    def account_read(self, account: dict) -> dict:
        activity = sum(
            (_d(t["amount"]) * (1 if t["type"] == "credit" else -1)
             for t in self.transactions if t["account_id"] == account["id"]),
            Decimal("0"),
        )
        return {**account, "balance": float(account["balance"]),
                "current_balance": float(account["balance"] + activity)}

    def list_transactions(self, query: dict) -> dict:
        rows = [
            t for t in self.transactions
            if t["account_id"] == query.get("account_id")
            and query.get("from", "0000") <= t["date"] <= query.get("to", "9999")
        ]
        limit, page = int(query.get("limit", 50)), int(query.get("page", 1))
        return {"items": rows[(page - 1) * limit: page * limit], "total": len(rows), "page": page, "limit": limit}

    # --- assets and the ledger (F4-F7) -------------------------------------------

    def create_asset(self, data: dict) -> dict:
        for asset in self.assets.values():
            if data.get("external_id") and asset["external_id"] == data["external_id"]:
                return self.asset_read(asset)  # F4
        asset = {
            "id": self._id(), "name": data["name"], "type": data["type"], "currency": data["currency"],
            "external_id": data.get("external_id"), "ticker": data.get("ticker"),
            "valuation_method": data.get("valuation_method"), "is_archived": False,
        }
        self.assets[asset["id"]] = asset
        self.values[asset["id"]] = []
        self.trades[asset["id"]] = []
        return self.asset_read(asset)

    def units(self, asset_id: str) -> Decimal:
        return sum((_d(t["quantity"]) * (1 if t["kind"] == "buy" else -1) for t in self.trades[asset_id]), Decimal("0"))

    def asset_read(self, asset: dict) -> dict:
        buys = [t for t in self.trades[asset["id"]] if t["kind"] == "buy"]
        qty = sum((_d(t["quantity"]) for t in buys), Decimal("0"))
        cost = sum((_d(t["quantity"]) * _d(t["price"]) for t in buys), Decimal("0"))
        values = sorted(self.values[asset["id"]], key=lambda v: v["date"])
        return {
            **asset,
            "units": float(self.units(asset["id"])),
            "average_price": float(cost / qty) if qty else None,
            "current_value": values[-1]["amount"] if values else None,
            "transaction_count": len(self.trades[asset["id"]]),
        }

    def add_trade(self, asset: dict, data: dict) -> tuple[int, Any]:
        assert _d(data["quantity"]) > 0 and _d(data["price"]) >= 0
        trade = {"id": self._id(), "asset_id": asset["id"], "source": "manual", **data,
                 "quantity": float(data["quantity"]), "price": float(data["price"])}
        ledger = self.trades[asset["id"]] + [trade]
        held = Decimal("0")
        for row in sorted(ledger, key=lambda t: date.fromisoformat(t["date"])):  # stable: created order
            held += _d(row["quantity"]) * (1 if row["kind"] == "buy" else -1)
            if held < 0:
                return 422, {"detail": "Cannot sell more units than held at that date."}
        self.trades[asset["id"]].append(trade)
        return 201, self.asset_read(asset)
