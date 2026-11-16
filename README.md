# Interactive Brokers for Securo

A [Securo](https://github.com/securo-finance/securo) plugin that imports your Interactive Brokers account through the **Flex Web Service**. It is read-only: it can't place trades or move money.

Each sync brings in:

- **Cash**: one investment account per currency, with IBKR's ending cash as the balance
- **Cash activity**: deposits, withdrawals, dividends, interest, fees and taxes from the Statement of Funds (trades are left out of spending)
- **Open positions**: one asset per position, valued at IBKR's position value on the statement date
- **Trades**: the buys and sells behind each position, in the asset's transaction history, so its units always equal IBKR's position
- **Closed positions**: instruments you sold, including ones bought and sold within the statement, keep their trades (and realized gain) and are archived

Short positions (such as written options) get an asset and a value, but no trade history: Securo's ledger doesn't hold short positions.

The first sync covers the last 365 days. Later syncs pick up from the last statement. IBKR finalizes Flex data once a day, so the newest day you'll see is yesterday.

## Set up IBKR

1. In the IBKR Portal, go to **Performance & Reports → Flex Queries**.
2. Under **Flex Web Service Configuration**, enable the service and generate a **token**. If you restrict it by IP, allow your Securo server's public IP.
3. Create an **Activity Flex Query** with these settings:
   - **Format: XML** (Text/CSV won't work)
   - **Period**: any. The plugin always asks for its own date range.
   - Sections and fields:

| Section | Options | Fields to select |
|---|---|---|
| Account Information | | Account ID, Currency, Account Alias or Name |
| Cash Report | Currency breakout | Currency, Ending Cash |
| Statement of Funds | | Currency, Date, Amount (or Debit and Credit), Activity Description (or Activity Code), Transaction ID, Trade ID, Symbol, Balance |
| Open Positions | **Summary** level of detail, not lots | Conid, Symbol, Description, Asset Class, Sub Category, Currency, Position, Multiplier, Mark Price, Position Value, Cost Basis Money, ISIN, Side, Open Date/Time |
| Trades (recommended) | Execution level | Conid, Symbol, Description, Asset Class, Sub Category, Currency, Trade ID, Trade Date, Quantity, Trade Price, Multiplier, Buy/Sell, Open/Close Indicator, Proceeds, Net Cash, IB Commission, IB Commission Currency, Taxes, Realized P/L (FIFO) |

4. Save the query and note its **Query ID**.

## Set up Securo

Your Securo instance needs plugins with code turned on (`PLUGINS_ENABLED=true` and the `plugins` Compose profile). See [Securo's plugin docs](https://docs.usesecuro.com/docs/features/plugins).

1. As the workspace owner, install **Interactive Brokers** from the plugin catalog.
2. Add a connection and enter the **Flex Web Service token** and the **Activity Flex Query ID**.
3. Click **Sync now**. A large statement can take IBKR a few minutes to generate. If it isn't ready, the sync finishes and the next one collects it.

The plugin creates ordinary records in your workspace: one investment account per currency (named "<account> Cash (USD)") and one asset per instrument. You can rename them; don't delete the IBKR entries in an asset's history, or the next sync adds them back. Each sync sets the account's opening balance so its balance equals IBKR's ending cash.

## What it can reach

- `ndcdyn.interactivebrokers.com` and `gdcdyn.interactivebrokers.com`, to request and download your statement
- Your Securo workspace's accounts, transactions and assets, through a token that only covers those

Your token is stored encrypted by Securo and only passed to this plugin.

## Development

```bash
uv run --python 3.14 --group dev pytest -q
```

The plugin itself uses only the Python standard library. [DESIGN.md](DESIGN.md) explains how it maps a statement onto Securo's API and why.

## License

MIT
