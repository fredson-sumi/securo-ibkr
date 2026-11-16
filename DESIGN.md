# Design notes

Why the plugin works the way it does. Code comments cite these as `F<n>` (API findings) and `D<n>` (decisions).

## What Securo's API does (tested on Securo v0.19.0 with a connection's scoped token)

| # | Finding | Effect on the plugin |
|---|---|---|
| F1 | `POST /api/accounts` ignores `external_id` and sets no `connection_id`. | Which account mirrors which IBKR currency lives in the connection's `state["accounts"]`. With state lost, an account is adopted by its exact name, `"<label> Cash (<CCY>)"`. |
| F2 | A manual account's `balance` is an opening-balance transaction dated `balance_date`. `current_balance` is that opening plus every other transaction. `PATCH balance` moves the opening. | Setting `balance` to IBKR's ending cash would count imported activity twice. See D10. |
| F3 | `POST /api/transactions` stores `external_id`, but posting the same one again creates a duplicate. | The plugin lists the account's transactions in the window and skips known `external_id`s. |
| F4 | `POST /api/assets` with an `external_id` that already exists returns the existing asset. | Creating an asset doubles as adopting it. |
| F5 | `current_value` on create adds a value dated today. A second `POST /values` on the same date adds a second row. `DELETE /values/{id}` exists. | Assets are created without a value. One value is written per statement date (D11). |
| F6 | The trade ledger owns `units`. `PATCH units` is ignored once trades exist, and a sell beyond the units held at that date is rejected (422). Short positions aren't supported. | Units come only from ledger entries, posted in date order (D12). |
| F7 | Asset trades take `notes` and return them. They have no `external_id`. | The note `IBKR trade <tradeID>` identifies an execution (D12). |
| F8 | `PATCH is_archived` works. A request outside the declared `api_scopes` gets 403. | Closed positions are archived. The declared scopes are enough. |

## Decisions

| # | Decision | Why |
|---|---|---|
| D3 | Public plugin. No tokens, query ids, account numbers or real amounts in the repo; every fixture is made up. | Anyone can read the code. |
| D7 | Standard-library Python only, no lockfile. External-id formats match Securo's former built-in IBKR provider: accounts `"<account>:<CCY>"`, transactions `"ibkr:<account>:<CCY>:<transactionID>"`, assets `"<account>:<model>:<conid>"`. | No wheels to vendor. Records made by the built-in provider can be adopted. |
| D8 | Trades go into Securo's asset-trade ledger (`POST /api/assets/{id}/transactions`). | Portfolio performance and realized gains need trades. |
| D10 | Each sync sets the cash account's opening balance to IBKR's ending cash minus all other transactions in the account, dated the day before the first imported day. | F2. The account's balance then equals IBKR's ending cash, and manual edits are absorbed into the opening. |
| D11 | One asset value per statement end date, replaced when it changed. | F5. No duplicate rows. |
| D12 | **Ledger reconciliation.** An empty ledger gets an opening buy (`IBKR opening position`) for the quantity held before the statement, priced at IBKR's cost basis. Executions follow in date order, skipped when their note is already there. Whatever still differs from IBKR's position (splits, transfers, corporate actions) becomes one `IBKR position adjustment` at the mark price on the statement date. A later sell the ledger can't cover gets an adjustment buy the day before it. | F6 and F7. Units always equal IBKR's position. |
| D13 | Statement of Funds trade rows carry no multiplier, so they use the matched position's. | Without it an option's units are 1/100 of the real number and the fee absorbs the difference. |
| D15 | The first sync covers 365 days, later syncs continue from the last imported day. Polling: 8 s apart, 40 s per call, references kept for 6 h, an invalid reference (Flex error 1017) replaced in the same call. | IBKR generates statements asynchronously, and a plugin call is stopped at 60 s. |
| D16 | Instruments traded and closed within a statement become assets too, with their trades, a final value of 0, and archived. Their opening buy is priced at the cost IBKR matched (from `fifoPnlRealized`). Currency conversions (asset class `CASH`) are not instruments. | Realized gains belong in the portfolio. Needs the Trades section. |
| D17 | Short positions get an asset and a value, but no ledger entries. | F6. |
| D18 | A Trades row without Proceeds and Net Cash keeps `tradePrice` as the per-unit price. | Quantity is already in units (contracts × multiplier). |
| D19 | Mapped records are re-read by id and created only when missing. No new write starts more than 50 s into a call. A sync that runs out of time keeps its mappings, notes `last_sync.incomplete`, and the next sync fetches the same days again. | Every write is idempotent, so redoing a window is safe. |
| D20 | Cash activity gets no payee. The description says what it is. | Setting one needs a payees scope. |
