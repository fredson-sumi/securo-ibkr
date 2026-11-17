# Design notes

Why the plugin works the way it does. Code comments cite these as `D<n>`.

Since 0.2 the plugin is a provider plugin: `sync` returns the statement's records (contract 1), and Securo writes them through the same pipeline its native bank connections use, matching each record by `external_id`. 0.1 wrote ordinary records through Securo's REST API; what that took (and why it fell short of a connected bank) is described in [securo-finance/securo#1461](https://github.com/securo-finance/securo/issues/1461).

## Decisions

| # | Decision | Why |
|---|---|---|
| D3 | Public plugin. No tokens, query ids, account numbers or real amounts in the repo; every fixture is made up. | Anyone can read the code. |
| D7 | Standard-library Python only, no lockfile. External-id formats: accounts `"<account>:<CCY>"`, transactions `"ibkr:<account>:<CCY>:<transactionID>"`, holdings `"<account>:<model>:<conid>"`, trades `"trade:<tradeID>"`. | No wheels to vendor. Securo matches every record by its external id, so a format that changed would duplicate what is already synced. |
| D13 | Statement of Funds trade rows carry no multiplier, so they use the matched position's. | Without it an option's units are 1/100 of the real number and the fee absorbs the difference. |
| D15 | The first sync covers 365 days, later syncs continue from the last imported day. Polling: 8 s apart, 40 s per call, references kept for 6 h, an invalid reference (Flex error 1017) replaced in the same call. While IBKR is still generating, `sync` returns `data: null`, and the statement collected later counts for the days it was asked for, even if a new day began meanwhile. | IBKR generates statements asynchronously, and a plugin call is stopped at 60 s. |
| D16 | Instruments traded and closed within a statement are holdings too, with quantity 0, value 0 and their trades. `state["closed"]` keeps each one, and every later statement lists it again (quantity 0, `trades: null`) until it is held again. Currency conversions (asset class `CASH`) are not instruments. | Realized gains belong in the portfolio. Securo skips a new holding that arrives already withdrawn, so quantity 0 is how it gets its trades, and it archives a holding its provider stops listing, which would take a sold position off the Assets page. Needs the Trades section. |
| D17 | Short positions are holdings with a negative quantity and no trades (`trades: null`). | Securo's trade ledger holds no short positions. |
| D18 | A Trades row without Proceeds and Net Cash keeps `tradePrice` as the per-unit price. | Quantity is already in units (contracts × multiplier). |
| D20 | Cash activity's payee is the row's symbol, or "Interactive Brokers". | Without a symbol, the broker is who the cash moved with. |
| D21 | The first statement opens what was held before it: one buy, `opening:<holding>`, dated the day before the statement (or before the first trade), for enough units to end at the position and cover every sell. It is priced at what those units cost: IBKR's cost basis at the end of the statement, less what the statement's buys added, plus what its sells took out (their proceeds less `fifoPnlRealized`). When a sell has no `fifoPnlRealized`, the price is IBKR's average cost now (or, for an instrument closed within the statement, the cost IBKR matched on a sell that has one). `state["openings"]` keeps it and every later statement sends it again; later statements never add another. | Securo stores a holding's trades only when they add up to the reported position. Without the opening buy, a position bought before the statement would have no trades. |
| D22 | A state from 0.1 (it has `last_report_end` but no `openings`) starts over: a full year, with opening buys. | 0.1's records were written through the API, not synced, so the synced records start empty. |
| D23 | Amounts are sent as plain strings with at most 8 decimals. | The contract refuses floats and longer fractions; Securo stores 6. |
| D24 | Each holding names its type from IBKR's asset class: STK is `stock` (`etf` when its sub-category is ETF), FUND is `fund`, CRYPTO is `crypto`, anything else `investment`. | Securo picks the asset's icon from it, and keeps a type the user changed by hand. |
