# Design notes

Why the plugin works the way it does. Code comments cite these as `D<n>`.

Since 0.2 the plugin is a provider plugin: `sync` returns the statement's records (contract 1), and Securo writes them through the same pipeline its native bank connections use, matching each record by `external_id`. 0.1 wrote ordinary records through Securo's REST API; what that took (and why it fell short of a connected bank) is described in [securo-finance/securo#1461](https://github.com/securo-finance/securo/issues/1461).

## Decisions

| # | Decision | Why |
|---|---|---|
| D3 | Public plugin. No tokens, query ids, account numbers or real amounts in the repo; every fixture is made up. | Anyone can read the code. |
| D7 | Standard-library Python only, no lockfile. External-id formats match Securo's former built-in IBKR provider: accounts `"<account>:<CCY>"`, transactions `"ibkr:<account>:<CCY>:<transactionID>"`, holdings `"<account>:<model>:<conid>"`, trades `"trade:<tradeID>"`. | No wheels to vendor. A connection that replaces the built-in provider matches its records one to one. |
| D13 | Statement of Funds trade rows carry no multiplier, so they use the matched position's. | Without it an option's units are 1/100 of the real number and the fee absorbs the difference. |
| D15 | The first sync covers 365 days, later syncs continue from the last imported day. Polling: 8 s apart, 40 s per call, references kept for 6 h, an invalid reference (Flex error 1017) replaced in the same call. While IBKR is still generating, `sync` returns `data: null`. | IBKR generates statements asynchronously, and a plugin call is stopped at 60 s. |
| D16 | Instruments traded and closed within a statement are holdings too, with quantity 0, value 0 and their trades. Securo archives them once a later statement no longer lists them. Currency conversions (asset class `CASH`) are not instruments. | Realized gains belong in the portfolio. Securo skips a new holding that arrives already withdrawn, so quantity 0 is how it gets its trades. Needs the Trades section. |
| D17 | Short positions are holdings with a negative quantity and no trades (`trades: null`). | Securo's trade ledger holds no short positions. |
| D18 | A Trades row without Proceeds and Net Cash keeps `tradePrice` as the per-unit price. | Quantity is already in units (contracts × multiplier). |
| D20 | Cash activity's payee is the row's symbol, or "Interactive Brokers". | The same as the built-in provider. |
| D21 | The first statement opens what was held before it: one buy, `opening:<holding>`, dated the day before the statement (or before the first trade), for enough units to end at the position and cover every sell, priced at IBKR's cost basis per unit (or, for an instrument closed within the statement, the cost IBKR matched in `fifoPnlRealized`). `state["openings"]` keeps it and every later statement sends it again; later statements never add another. | Securo stores a holding's trades only when they add up to the reported position. Without the opening buy, a position bought before the statement would have no trades. |
| D22 | A state from 0.1 (it has `last_report_end` but no `openings`) starts over: a full year, with opening buys. | 0.1's records were written through the API, not synced, so the synced records start empty. |
| D23 | Amounts are sent as plain strings with at most 8 decimals. | The contract refuses floats and longer fractions; Securo stores 6. |
