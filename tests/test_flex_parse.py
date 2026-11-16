"""Parser and error-mapping tests, ported from Securo's test_providers_ibkr.py."""
from __future__ import annotations

from decimal import Decimal

import pytest
from tests.fake_report import report_xml

from securo_ibkr.errors import FlexPending, FlexTransient, NeedsAuth
from securo_ibkr.flex import (
    USER_ACTION_ERRORS,
    USER_ACTION_MESSAGES,
    detect_non_xml,
    parse_report,
    raise_for_flex_error,
    statement_amount,
)

# --- parser (ported) ---------------------------------------------------------


def test_parse_report_requires_one_account():
    with pytest.raises(NeedsAuth, match="exactly one account") as exc:
        parse_report(report_xml(second_account=True))
    assert exc.value.code == "flex_query_account_count"


def test_parse_report_requires_summary_positions():
    with pytest.raises(NeedsAuth, match="Summary") as exc:
        parse_report(report_xml(position_detail="LOT"))
    assert exc.value.code == "flex_query_position_detail"


def test_single_currency_base_summary_maps_to_account_base_currency():
    xml = (
        report_xml()
        .replace('        <CashReportCurrency currency="USD" endingCash="1000.25" />\n', "")
        .replace('        <CashReportCurrency currency="EUR" endingCash="-50.50" />\n', "")
    )

    report = parse_report(xml)

    assert report.cash_balances == {"USD": Decimal("1100")}


def test_parses_account_and_sections():
    report = parse_report(report_xml().encode("utf-8"))

    assert report.account_id == "U1234567"
    assert report.account_alias == "Long Term"
    assert report.account_name == "Example Account"
    assert report.base_currency == "USD"
    # BASE_SUMMARY is ignored next to native currency rows.
    assert report.cash_balances == {"USD": Decimal("1000.25"), "EUR": Decimal("-50.50")}
    assert len(report.statement_funds) == 5
    assert len(report.positions) == 2
    assert report.trades == []
    assert report.from_date is None


def test_query_validation_reports_the_specific_missing_field():
    xml = report_xml().replace(' tradeID=""', "")

    with pytest.raises(NeedsAuth) as exc:
        parse_report(xml)

    assert exc.value.code == "flex_query_missing_field"
    assert "Trade ID" in str(exc.value)


def test_rejects_duplicate_summary_positions():
    duplicate = """
        <OpenPosition accountId="U1234567" model="Core" conid="100001"
          currency="USD" assetCategory="STK" symbol="EXMP"
          description="EXAMPLE CORP" position="3" multiplier="1" markPrice="220"
          positionValue="660" costBasisMoney="500" isin="US0000000001"
          side="Long" openDateTime="20250102 093000" levelOfDetail="SUMMARY" />
    """
    xml = report_xml().replace("      </OpenPositions>", duplicate + "      </OpenPositions>")

    with pytest.raises(NeedsAuth, match="duplicate Summary"):
        parse_report(xml)


def test_rejects_non_finite_position_numbers():
    xml = report_xml().replace('positionValue="660"', 'positionValue="NaN"', 1)

    with pytest.raises(ValueError, match="finite number"):
        parse_report(xml)


def test_rejects_malformed_statement_and_position_dates_during_parse():
    malformed_funds = report_xml().replace('date="20260810"', 'date="not-a-date"', 1)
    with pytest.raises(ValueError, match="Statement of Funds / Date"):
        parse_report(malformed_funds)

    malformed_position = report_xml().replace(
        'openDateTime="20250102;093000"', 'openDateTime="not-a-date"', 1
    )
    with pytest.raises(ValueError, match="Open Positions / Open Date"):
        parse_report(malformed_position)


# --- parser (added: guards the port keeps) ----------------------------------


def test_malformed_xml_is_transient():
    with pytest.raises(FlexTransient, match="malformed"):
        parse_report("<FlexQueryResponse><FlexStatements>")


def test_rejects_doctype_and_entities():
    xml = report_xml().replace(
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "boom">]>',
    )
    with pytest.raises(ValueError, match="unsupported XML declarations"):
        parse_report(xml)


@pytest.mark.parametrize(
    ("section", "code"),
    [
        ("AccountInformation", "flex_query_missing_section"),
        ("StmtFunds", "flex_query_missing_section"),
        ("OpenPositions", "flex_query_missing_section"),
    ],
)
def test_missing_section_needs_auth(section, code):
    xml = report_xml().replace(f"<{section}", f"<X{section}").replace(f"</{section}>", f"</X{section}>")
    with pytest.raises(NeedsAuth) as exc:
        parse_report(xml)
    assert exc.value.code == code


def test_reads_trades_section_executions_only():
    trades = (
        "<Trades>"
        '<Trade conid="100001" tradeID="T-1" levelOfDetail="EXECUTION" />'
        '<Trade conid="100001" tradeID="T-2" levelOfDetail="ORDER" />'
        '<Trade conid="100001" tradeID="T-3" />'
        "</Trades>"
    )
    xml = report_xml().replace("    </FlexStatement>\n", trades + "    </FlexStatement>\n", 1)
    xml = xml.replace(
        '<FlexStatement accountId="U1234567"',
        '<FlexStatement fromDate="20250817" accountId="U1234567"',
    )

    report = parse_report(xml)

    assert [row["tradeID"] for row in report.trades] == ["T-1", "T-3"]
    assert str(report.from_date) == "2025-08-17"


def test_statement_amount_prefers_amount_then_credit_minus_debit():
    assert statement_amount({"amount": "-1,000.50"}) == Decimal("-1000.50")
    assert statement_amount({"credit": "10", "debit": "-3"}) == Decimal("7")
    assert statement_amount({"debit": "3"}) == Decimal("-3")
    with pytest.raises(ValueError, match="Amount or Debit/Credit"):
        statement_amount({"amount": ""})


# --- error mapping -----------------------------------------------------------


def test_expired_token_requires_user_action():
    with pytest.raises(NeedsAuth) as exc:
        raise_for_flex_error("1012", "Token expired")
    assert exc.value.code == "credentials_expired"


@pytest.mark.parametrize("code", sorted(USER_ACTION_ERRORS))
def test_user_action_codes_keep_safe_message_and_code(code):
    with pytest.raises(NeedsAuth) as exc:
        raise_for_flex_error(code, "upstream do-not-leak text")
    assert exc.value.code == USER_ACTION_ERRORS[code]
    assert str(exc.value).startswith(USER_ACTION_MESSAGES[code])
    assert "do-not-leak" not in str(exc.value)


def test_rate_limit_is_pending_and_upstream_text_is_not_in_error():
    with pytest.raises(FlexPending, match="rate limit reached") as exc:
        raise_for_flex_error("1018", "Too many requests for do-not-leak")
    assert "do-not-leak" not in str(exc.value)


def test_still_generating_is_pending():
    with pytest.raises(FlexPending, match="still generating"):
        raise_for_flex_error("1019", "Statement generation in progress")


@pytest.mark.parametrize("code", ["1001", "1017", "1021", "9999", None, "t=do-not-leak"])
def test_other_codes_are_transient(code):
    with pytest.raises(FlexTransient) as exc:
        raise_for_flex_error(code, "do-not-leak")
    assert "do-not-leak" not in str(exc.value)


def test_text_report_requires_xml_query_output():
    with pytest.raises(NeedsAuth) as exc:
        detect_non_xml(b"ClientAccountID,Currency,EndingCash\nU1234567,USD,100.00")
    assert exc.value.code == "flex_query_output_format"
    assert "must be XML" in str(exc.value)


def test_other_non_xml_is_transient():
    with pytest.raises(FlexTransient, match="non-XML"):
        detect_non_xml(b"temporarily unavailable")


@pytest.mark.parametrize("content", [b"", b"\xef\xbb\xbf  <FlexQueryResponse/>", b"\n<x/>"])
def test_xml_or_empty_passes_non_xml_check(content):
    assert detect_non_xml(content) is None
