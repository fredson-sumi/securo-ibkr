"""Securo calls `plugin.sync(params, context)` in its plugin runner.

- `context.secrets["token"]`: the Flex Web Service token
- `context.config["query_id"]`: the Activity Flex Query id

`sync` returns `{"state": ..., "data": ...}`. `data` holds the records for
Securo to write as a connected bank (`payload`), or is null while IBKR is
still generating the statement. Securo keeps the returned `state` only once
it has written the records, so a sync that fails fetches the same days again.

`params["state"]` is what the last sync returned (`{}` the first time):

- `flex`: a statement IBKR is still generating (see `fetch.Pending`)
- `last_report_end`: the last statement day already imported
- `openings`: the opening buy sent for each instrument held before the
  first statement, sent again with every statement so it stays in place
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from securo_ibkr import fetch as flex_fetch
from securo_ibkr import payload
from securo_ibkr.errors import FlexPending, NeedsAuth

_QUERY_ID = re.compile(r"^[0-9]{1,32}$")
INITIAL_HISTORY_DAYS = 365


def _credentials(context: Any) -> tuple[str, str]:
    token = str((context.secrets or {}).get("token") or "").strip()
    query_id = str((context.config or {}).get("query_id") or "").strip()
    if not token:
        raise NeedsAuth("The Flex Web Service token is missing.")
    if len(token) > 4096:
        raise NeedsAuth("The Flex Web Service token is too long.")
    if not _QUERY_ID.fullmatch(query_id):
        raise NeedsAuth("The Activity Flex Query ID must contain only digits.", code="flex_query_invalid")
    return token, query_id


def report_window(state: dict[str, Any], today: date) -> tuple[date, date] | None:
    """The days to ask IBKR for, or None when yesterday is already imported.

    IBKR finalizes Activity Flex data once a day, and asking for the still-open
    current day can leave generation stuck, so a statement always ends yesterday.
    """
    end = today - timedelta(days=1)
    initial_start = end - timedelta(days=INITIAL_HISTORY_DAYS - 1)
    last = state.get("last_report_end")
    start = date.fromisoformat(last) + timedelta(days=1) if last else initial_start
    start = max(start, initial_start)
    if start > end:
        return None
    return start, end


class IbkrFlex:
    def __init__(
        self,
        *,
        http: flex_fetch.Http | None = None,
        today: Callable[[], date] = lambda: datetime.now(timezone.utc).date(),
        fetch_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._http = http
        self._today = today
        self._fetch_kwargs = fetch_kwargs or {}

    def sync(self, params: dict[str, Any], context: Any) -> dict[str, Any]:
        token, query_id = _credentials(context)
        state = dict(params.get("state") or {})
        pending = flex_fetch.Pending.from_state(state.get("flex"))
        window = report_window(state, self._today())
        if window is None and pending is None:
            return {"state": state, "data": None}
        start, end = window or (pending.from_date, pending.to_date)  # type: ignore[union-attr]
        kwargs = dict(self._fetch_kwargs)
        if self._http is not None:
            kwargs["http"] = self._http
        try:
            outcome = flex_fetch.fetch(
                token=token, query_id=query_id, start=start, end=end, pending=pending, **kwargs
            )
        except FlexPending:
            # Rate limited or still generating before a reference was issued: try next sync.
            state.pop("flex", None)
            return {"state": state, "data": None}
        if outcome.report is None:
            state["flex"] = outcome.pending.to_state()  # type: ignore[union-attr]
            return {"state": state, "data": None}
        state.pop("flex", None)
        data = payload.build(outcome.report, start, end, state)
        state["last_report_end"] = end.isoformat()
        return {"state": state, "data": data}


plugin = IbkrFlex()
