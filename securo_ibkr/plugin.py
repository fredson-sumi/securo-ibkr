"""Securo calls `plugin.sync(params, context)` in its plugin runner.

- `context.secrets["token"]`: the Flex Web Service token
- `context.config["query_id"]`: the Activity Flex Query id
- `context.securo`: `api_url`, `workspace_id`, `connection_id` and a `token`
  limited to the manifest's `api_scopes`

`params["state"]` is what the last sync returned (`{}` the first time):

- `flex`: a statement IBKR is still generating (see `fetch.Pending`)
- `last_report_end`: the last statement day already imported
- `accounts`, `assets`: which Securo records mirror which IBKR ones
- `last_sync`: what the last import wrote, or `incomplete` when the call's
  time ran out first (the next sync fetches the same days and finishes)
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from securo_ibkr import fetch as flex_fetch
from securo_ibkr.api import OutOfTime, SecuroApi, Transport, urllib_transport
from securo_ibkr.errors import FlexPending, NeedsAuth
from securo_ibkr.flex import ParsedReport
from securo_ibkr.writer import write_report

_QUERY_ID = re.compile(r"^[0-9]{1,32}$")
INITIAL_HISTORY_DAYS = 365
#: Seconds into a call after which no new write starts (the runner stops a call at 60).
WRITE_BUDGET = 50


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
        api_transport: Transport = urllib_transport,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http
        self._today = today
        self._fetch_kwargs = fetch_kwargs or {}
        self._api_transport = api_transport
        self._clock = clock

    def sync(self, params: dict[str, Any], context: Any) -> dict[str, Any]:
        started = self._clock()
        token, query_id = _credentials(context)
        state = dict(params.get("state") or {})
        pending = flex_fetch.Pending.from_state(state.get("flex"))
        window = report_window(state, self._today())
        if window is None and pending is None:
            return {"state": state}
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
            return {"state": state}
        if outcome.report is None:
            state["flex"] = outcome.pending.to_state()  # type: ignore[union-attr]
            return {"state": state}
        state.pop("flex", None)
        api = SecuroApi(
            str(context.securo.get("api_url") or ""),
            str(context.securo.get("token") or ""),
            transport=self._api_transport,
            deadline=started + WRITE_BUDGET,
            clock=self._clock,
        )
        self.apply(api, outcome.report, state, start, end)
        return {"state": state}

    def apply(self, api: SecuroApi, report: ParsedReport, state: dict[str, Any], start: date, end: date) -> None:
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            summary = write_report(api, report, state, start, end)
        except OutOfTime:
            state["last_sync"] = {"at": stamp, "incomplete": f"{start}..{end}"}
            return
        state["last_report_end"] = end.isoformat()
        state["last_sync"] = {"at": stamp, "from": start.isoformat(), "to": end.isoformat(), **summary.to_state()}


plugin = IbkrFlex()
