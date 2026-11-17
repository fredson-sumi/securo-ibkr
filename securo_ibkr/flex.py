"""Parse an IBKR Activity Flex XML statement and map Flex error codes.

Standard library only. Nothing here talks to the network: the fetch lives
elsewhere.

Exceptions:
- `NeedsAuth`: the person has to fix the token, the query or IBKR settings.
- `FlexPending`: the statement isn't ready yet (1019) or IBKR throttled us (1018).
- `FlexTransient`: malformed or unexpected responses and other IBKR failures.
- `ValueError`: a selected field holds a value we can't read (bad number or date).

No exception message ever carries IBKR's ErrorMessage text or the token.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, NoReturn
from xml.etree import ElementTree

from securo_ibkr.errors import FlexNotAvailable, FlexPending, FlexTransient, NeedsAuth

MAX_RESPONSE_BYTES = 20 * 1024 * 1024

_DATE_PREFIX_RE = re.compile(
    r"^(\d{8}|\d{4}-\d{2}-\d{2}|\d{4}/\d{2}/\d{2}|\d{2}/\d{2}/\d{4})(?:$|[ T;])"
)

USER_ACTION_ERRORS = {
    "1010": "flex_query_invalid",
    "1011": "flex_service_inactive",
    "1012": "credentials_expired",
    "1013": "flex_ip_restricted",
    "1014": "flex_query_invalid",
    "1015": "credentials_invalid",
    "1016": "flex_account_invalid",
    "1020": "flex_request_invalid",
}
USER_ACTION_MESSAGES = {
    "1010": "The saved query is a legacy Flex Query. Convert it to an Activity Flex Query.",
    "1011": "IBKR Flex Web Service is inactive. Enable it and reconnect.",
    "1012": "The IBKR Flex token has expired. Generate a new token and reconnect.",
    "1013": "IBKR rejected this server's IP address. Update the token IP restriction.",
    "1014": "The IBKR Activity Flex Query ID is invalid. Verify it and reconnect.",
    "1015": "The IBKR Flex token is invalid. Generate a new token and reconnect.",
    "1016": "The account selected by this IBKR Flex Query is invalid.",
    "1020": "IBKR could not validate the Flex request. Verify the token and query ID.",
}
TRANSIENT_ERRORS = frozenset(
    {"1001", "1003", "1004", "1005", "1006", "1007", "1008", "1009", "1017", "1021"}
)
# GetStatement codes worth polling again with the same reference.
POLL_RETRY_ERRORS = TRANSIENT_ERRORS - {"1017"} | {"1018", "1019"}


@dataclass(frozen=True)
class ParsedReport:
    account_id: str
    account_alias: str | None
    account_name: str | None
    base_currency: str
    cash_balances: dict[str, Decimal]
    statement_funds: list[dict[str, str]]
    positions: list[dict[str, str]]
    # Execution rows from the optional Trades section.
    trades: list[dict[str, str]] = field(default_factory=list)
    # First day the statement covers.
    from_date: date | None = None


# --- error mapping -----------------------------------------------------------


def raise_for_flex_error(code: str | None, message: str | None = None) -> NoReturn:
    """Raise the exception for a Flex `<ErrorCode>`.

    `message` (IBKR's ErrorMessage) is accepted but never used: an
    intermediary could echo request parameters, including the token.
    """
    del message
    if code in USER_ACTION_ERRORS:
        raise NeedsAuth(USER_ACTION_MESSAGES[code], code=USER_ACTION_ERRORS[code])
    if code == "1018":
        raise FlexPending("IBKR Flex rate limit reached. Try again later.")
    if code == "1019":
        raise FlexPending("IBKR Flex is still generating the statement.")
    if code == "1003":
        raise FlexNotAvailable("IBKR Flex has no statement for these days yet (error 1003). Try again later.")
    if code == "1025":
        # Every refused request counts toward this block, so say what it is.
        raise FlexTransient(
            "IBKR is refusing this token for a while after too many failed requests (error 1025). "
            "Wait a few hours before syncing again."
        )
    if code in TRANSIENT_ERRORS:
        raise FlexTransient(
            f"IBKR Flex could not generate the statement (error {code}). Try again later."
        )
    # Unknown codes should not permanently break a healthy connection.
    safe_code = code if code and code.isdigit() and len(code) <= 8 else "unknown"
    raise FlexTransient(f"IBKR Flex returned error {safe_code}. Try again later.")


def detect_non_xml(content: bytes) -> None:
    """Raise if a GetStatement body is not XML; return None otherwise.

    A Text/CSV query output needs the person to switch the query to XML.
    """
    prefix = content.lstrip(b"\xef\xbb\xbf \t\r\n")
    if not prefix or prefix.startswith(b"<"):
        return
    first_line = prefix.splitlines()[0][:4096]
    if any(delimiter in first_line for delimiter in (b",", b"\t", b";")):
        raise NeedsAuth(
            "The Activity Flex Query output format must be XML, not Text/CSV. "
            "Edit the saved query in IBKR, select XML, and reconnect.",
            code="flex_query_output_format",
        )
    raise FlexTransient("IBKR Flex returned an unexpected non-XML response")


# --- XML helpers -------------------------------------------------------------


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(root: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [element for element in root.iter() if local_name(element.tag) == name]


def _first(root: ElementTree.Element, *names: str) -> ElementTree.Element | None:
    wanted = set(names)
    return next(
        (element for element in root.iter() if local_name(element.tag) in wanted),
        None,
    )


def child_text(root: ElementTree.Element, *names: str) -> str | None:
    element = _first(root, *names)
    if element is None or element.text is None:
        return None
    text = element.text.strip()
    return text or None


def _attrs(element: ElementTree.Element) -> dict[str, str]:
    return {key.rsplit("}", 1)[-1]: item for key, item in element.attrib.items()}


def _xml_bytes(payload: str | bytes) -> bytes:
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("IBKR Flex response is too large")
    upper = raw.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError("IBKR Flex report contains unsupported XML declarations")
    return raw


def parse_element(payload: str | bytes) -> ElementTree.Element:
    """Parse XML after the size and DOCTYPE/ENTITY guards."""
    return ElementTree.fromstring(_xml_bytes(payload))


# --- value helpers -----------------------------------------------------------


def field_value(data: dict[str, str], *keys: str) -> str | None:
    """First non-blank value among `keys`, stripped."""
    for key in keys:
        raw = data.get(key)
        if raw is not None and str(raw).strip() != "":
            return str(raw).strip()
    return None


def has_field(data: dict[str, str], *keys: str) -> bool:
    """Whether the query selected at least one equivalent XML field.

    IBKR emits selected-but-empty attributes as ``field=""``, so key presence
    tells an unavailable value (an option without an ISIN) from a field the
    person did not add to the Flex query at all.
    """
    return any(key in data for key in keys)


def _missing_selected_fields(data: dict[str, str], fields: dict[str, tuple[str, ...]]) -> list[str]:
    return [label for label, aliases in fields.items() if not has_field(data, *aliases)]


def _query_error(message: str, *, code: str = "flex_query_missing_field") -> NoReturn:
    raise NeedsAuth(message, code=code)


def parse_decimal(value: Any, *, field: str, required: bool = False) -> Decimal | None:
    if value is None or str(value).strip() in {"", "--"}:
        if required:
            raise ValueError(f"IBKR Flex report is missing required field: {field}")
        return None
    try:
        parsed = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"IBKR Flex field {field} is not a valid number") from exc
    if not parsed.is_finite():
        raise ValueError(f"IBKR Flex field {field} is not a finite number")
    return parsed


def parse_date(value: Any, *, field: str, required: bool = False) -> date | None:
    if value is None or not str(value).strip():
        if required:
            raise ValueError(f"IBKR Flex report is missing required field: {field}")
        return None
    match = _DATE_PREFIX_RE.match(str(value).strip())
    if not match:
        raise ValueError(f"IBKR Flex field {field} is not a valid date")
    raw = match.group(1)
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"IBKR Flex field {field} is not a valid date")


def iso_currency(value: Any) -> str | None:
    code = str(value or "").strip().upper()
    if len(code) == 3 and code.isalpha():
        return code
    return None


def statement_amount(row: dict[str, str]) -> Decimal:
    """Signed cash amount of a Statement of Funds row (Amount, else Credit - Debit)."""
    signed = parse_decimal(field_value(row, "amount"), field="Statement of Funds / Amount")
    if signed is not None:
        return signed
    credit = parse_decimal(field_value(row, "credit"), field="Statement of Funds / Credit")
    debit = parse_decimal(field_value(row, "debit"), field="Statement of Funds / Debit")
    if credit is None and debit is None:
        raise ValueError("IBKR Flex Statement of Funds must include Amount or Debit/Credit")
    return (credit or Decimal("0")) - abs(debit or Decimal("0"))


# --- report parser -----------------------------------------------------------

_FUNDS_FIELDS = {
    "Currency": ("currency",),
    "Date": ("date", "reportDate"),
    "Amount or Debit/Credit": ("amount", "debit", "credit"),
    "Activity Description or Code": ("activityDescription", "description", "activityCode"),
    "Transaction ID": ("transactionID", "transactionId"),
    "Trade ID": ("tradeID", "tradeId"),
    "Symbol": ("symbol",),
    "Balance": ("balance",),
}
_POSITION_FIELDS = {
    "Conid": ("conid",),
    "Symbol": ("symbol",),
    "Description": ("description",),
    "Asset Class": ("assetCategory", "assetClass"),
    "Currency": ("currency",),
    "Position": ("position", "quantity"),
    "Multiplier": ("multiplier",),
    "Mark Price": ("markPrice",),
    "Position Value": ("positionValue",),
    "Cost Basis Money": ("costBasisMoney",),
    "ISIN": ("isin",),
    "Side": ("side",),
    "Open Date/Time": ("openDateTime",),
}


def parse_report(xml: str | bytes) -> ParsedReport:
    """Parse and validate the Activity Flex sections the plugin consumes."""
    try:
        root = parse_element(xml)
    except ElementTree.ParseError as exc:
        raise FlexTransient("IBKR Flex returned malformed report XML") from exc

    statements = _children(root, "FlexStatement")
    if len(statements) != 1:
        raise NeedsAuth(
            "The IBKR Flex query must return exactly one account. "
            "Create one Flex query per IBKR account.",
            code="flex_query_account_count",
        )
    statement = statements[0]
    statement_data = _attrs(statement)

    account_info = _first(statement, "AccountInformation")
    if account_info is None:
        _query_error(
            "The IBKR Flex query is missing the Account Information section.",
            code="flex_query_missing_section",
        )
    info = {**statement_data, **_attrs(account_info)}
    account_id = field_value(info, "accountId", "accountID")
    base_currency = iso_currency(field_value(info, "currency", "baseCurrency"))
    if not account_id or not base_currency:
        _query_error("Account Information must include Account ID and Currency.")
    if not has_field(info, "accountAlias", "acctAlias", "name", "accountName"):
        _query_error("Account Information must include Account Name or Account Alias.")

    cash_report = _first(statement, "CashReport")
    if cash_report is None:
        _query_error(
            "The IBKR Flex query is missing Cash Report with currency breakout.",
            code="flex_query_missing_section",
        )
    cash_balances: dict[str, Decimal] = {}
    base_summary: Decimal | None = None
    for row in cash_report.iter():
        if row is cash_report or local_name(row.tag) not in {"CashReportCurrency", "CashReportLine"}:
            continue
        data = _attrs(row)
        raw_currency = (field_value(data, "currency") or "").upper()
        ending_raw = field_value(data, "endingCash", "endingSettledCash")
        if raw_currency == "BASE_SUMMARY":
            base_summary = parse_decimal(ending_raw, field="Cash Report / Ending Cash")
            continue
        currency = iso_currency(raw_currency)
        if not currency:
            continue  # excludes segment totals
        ending = parse_decimal(ending_raw, field="Cash Report / Ending Cash", required=True)
        cash_balances[currency] = ending or Decimal("0")
    # Single-currency reports may hold only BASE_SUMMARY, already in the base
    # currency. Never use it next to native rows: it is then a converted total
    # that would double-count them.
    if not cash_balances and base_summary is not None:
        cash_balances[base_currency] = base_summary
    if not cash_balances:
        _query_error("Cash Report must include Currency and Ending Cash at currency-breakout level.")

    funds = _first(statement, "StmtFunds", "StatementOfFunds")
    if funds is None:
        _query_error(
            "The IBKR Flex query is missing Statement of Funds.",
            code="flex_query_missing_section",
        )
    statement_funds = [
        _attrs(row)
        for row in funds.iter()
        if row is not funds and local_name(row.tag) in {"StmtFund", "StatementOfFundsLine"}
    ]
    for row in statement_funds:
        missing = _missing_selected_fields(row, _FUNDS_FIELDS)
        if missing:
            _query_error(
                "Statement of Funds is missing required selected field(s): "
                + ", ".join(missing)
                + "."
            )
        transaction_id = field_value(row, "transactionID", "transactionId")
        is_trade = bool(field_value(row, "tradeID", "tradeId", "tradeQuantity"))
        if transaction_id and not is_trade:
            if not iso_currency(field_value(row, "currency")):
                raise ValueError("IBKR Flex Statement of Funds transaction has an invalid Currency")
            parse_date(
                field_value(row, "date", "reportDate"),
                field="Statement of Funds / Date",
                required=True,
            )
            statement_amount(row)

    open_positions = _first(statement, "OpenPositions")
    if open_positions is None:
        _query_error(
            "The IBKR Flex query is missing Open Positions.",
            code="flex_query_missing_section",
        )
    positions: list[dict[str, str]] = []
    position_keys: set[tuple[str, str, str]] = set()
    for row in open_positions.iter():
        if row is open_positions or local_name(row.tag) != "OpenPosition":
            continue
        data = _attrs(row)
        detail = (field_value(data, "levelOfDetail") or "SUMMARY").upper()
        if detail not in {"SUMMARY", "S"}:
            _query_error(
                "Open Positions must use Summary level of detail, not tax lots.",
                code="flex_query_position_detail",
            )
        missing = _missing_selected_fields(data, _POSITION_FIELDS)
        if missing:
            _query_error(
                "Open Positions is missing required selected field(s): " + ", ".join(missing) + "."
            )
        conid = field_value(data, "conid")
        currency = iso_currency(field_value(data, "currency"))
        model = field_value(data, "model") or "default"
        if not conid or not currency:
            _query_error("Open Positions contains a row without Conid or Currency.")
        for keys, label in (
            (("position", "quantity"), "Position"),
            (("multiplier",), "Multiplier"),
            (("markPrice",), "Mark Price"),
            (("positionValue",), "Position Value"),
            (("costBasisMoney",), "Cost Basis Money"),
        ):
            parse_decimal(field_value(data, *keys), field=f"Open Positions / {label}", required=True)
        open_date = field_value(data, "openDateTime")
        if open_date:
            parse_date(open_date, field="Open Positions / Open Date")
        position_key = (account_id, model, conid)
        if position_key in position_keys:
            _query_error(
                "Open Positions contains duplicate Summary rows for the same account, "
                "model, and conid. Disable lot-level or alternate grouping."
            )
        position_keys.add(position_key)
        positions.append(data)

    # Trades is optional: without it, executions come from the trade rows of
    # Statement of Funds when those carry quantity and price.
    trades_section = _first(statement, "Trades")
    trades = [
        _attrs(row)
        for row in (trades_section.iter() if trades_section is not None else ())
        if local_name(row.tag) == "Trade"
        and (field_value(_attrs(row), "levelOfDetail") or "EXECUTION").upper() == "EXECUTION"
    ]

    return ParsedReport(
        account_id=account_id,
        account_alias=field_value(info, "accountAlias", "acctAlias"),
        account_name=field_value(info, "name", "accountName"),
        base_currency=base_currency,
        cash_balances=cash_balances,
        statement_funds=statement_funds,
        positions=positions,
        trades=trades,
        from_date=parse_date(field_value(statement_data, "fromDate"), field="Statement / From Date"),
    )
