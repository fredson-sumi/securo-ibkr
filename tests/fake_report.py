"""Shared fake Flex statement. Every id, alias and amount here is made up (D3)."""
from __future__ import annotations


def report_xml(*, second_account: bool = False, position_detail: str = "SUMMARY") -> str:
    second = (
        '<FlexStatement accountId="U7654321"><AccountInformation accountId="U7654321" '
        'currency="USD"/><CashReport><CashReportCurrency currency="USD" '
        'endingCash="0"/></CashReport><StmtFunds/><OpenPositions/></FlexStatement>'
        if second_account
        else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse>
  <FlexStatements count="{2 if second_account else 1}">
    <FlexStatement accountId="U1234567" accountAlias="Long Term" currency="USD">
      <AccountInformation accountId="U1234567" accountAlias="Long Term"
        name="Example Account" currency="USD" />
      <CashReport>
        <CashReportCurrency currency="BASE_SUMMARY" endingCash="1100" />
        <CashReportCurrency currency="USD" endingCash="1000.25" />
        <CashReportCurrency currency="EUR" endingCash="-50.50" />
      </CashReport>
      <StmtFunds>
        <StmtFund currency="USD" date="20260810" transactionID="TX-DIV"
          tradeID=""
          activityCode="DIV" activityDescription="EXMP dividend" amount="12.34"
          symbol="EXMP" balance="1000.25" />
        <StmtFund currency="USD" date="20260811" transactionID="TX-FEE"
          tradeID=""
          activityCode="FEE" activityDescription="Market data fee" debit="5.00"
          symbol="" balance="995.25" />
        <StmtFund currency="USD" date="20260812" transactionID="TX-TRADE"
          tradeID="TR-1" tradeQuantity="2" activityDescription="Buy EXMP"
          amount="-400" symbol="EXMP" balance="595.25" />
        <StmtFund currency="EUR" date="2026-08-09" transactionID="TX-INT"
          tradeID="" activityCode="INT" activityDescription="Credit interest"
          credit="1.25" symbol="" balance="-50.50" />
        <StmtFund currency="USD" date="" transactionID="" tradeID=""
          activityCode="StartingCash" amount="900" symbol="" balance="900" />
      </StmtFunds>
      <OpenPositions>
        <OpenPosition accountId="U1234567" model="Core" conid="100001"
          currency="USD" assetCategory="STK" symbol="EXMP"
          description="EXAMPLE CORP" position="3" multiplier="1" markPrice="220"
          positionValue="660" costBasisMoney="500" isin="US0000000001"
          side="Long" openDateTime="20250102;093000" levelOfDetail="{position_detail}" />
        <OpenPosition accountId="U1234567" conid="100002" currency="USD"
          assetCategory="OPT" symbol="EXMP  260116P00200000"
          description="EXMP JAN 2026 200 Put" position="-1" multiplier="100"
          markPrice="3.5" positionValue="-350" costBasisMoney="-300"
          isin="" side="Short" openDateTime="" levelOfDetail="SUMMARY" />
      </OpenPositions>
    </FlexStatement>
    {second}
  </FlexStatements>
</FlexQueryResponse>"""
