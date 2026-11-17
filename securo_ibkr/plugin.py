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
- `closed`: the instruments sold out so far, listed again with every
  statement so Securo keeps showing them as sold
- `awaiting`: the day IBKR said it hasn't published yet, and when it was
  asked, so it isn't asked for again right away
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable

from securo_ibkr import fetch as flex_fetch
from securo_ibkr import payload
from securo_ibkr.errors import FlexNotAvailable, FlexPending, NeedsAuth

_QUERY_ID = re.compile(r"^[0-9]{1,32}$")
INITIAL_HISTORY_DAYS = 365
#: The hour (UTC) from which yesterday is asked for. IBKR says a day's
#: statement is available "around midnight (EST)", which is 05:00 UTC (D25).
PUBLISHED_FROM_HOUR = 6
#: How long a day IBKR still hasn't published is left alone before asking again (D25).
NOT_PUBLISHED_RETRY = timedelta(hours=4)


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


def report_window(state: dict[str, Any], today: date, now: datetime | None = None) -> tuple[date, date] | None:
    """The days to ask IBKR for, or None when there is nothing new to ask for.

    IBKR finalizes Activity Flex data once a day, and asking for the still-open
    current day can leave generation stuck, so a statement ends yesterday at
    the latest. IBKR publishes a day some hours after it ends, though (D25).
    Securo calls `sync` every hour around the clock, so with `now` the window
    leaves yesterday out until `PUBLISHED_FROM_HOUR`, and after that for as
    long as IBKR just said it doesn't have it.
    """
    end = today - timedelta(days=1)
    if now is not None and now < datetime.combine(today, time(PUBLISHED_FROM_HOUR), tzinfo=timezone.utc):
        end -= timedelta(days=1)
    initial_start = end - timedelta(days=INITIAL_HISTORY_DAYS - 1)
    if now is not None and _awaited_day(state, now) == end:
        end -= timedelta(days=1)
    last = state.get("last_report_end")
    start = date.fromisoformat(last) + timedelta(days=1) if last else initial_start
    start = max(start, initial_start)
    if start > end:
        return None
    return start, end


def _awaited_day(state: dict[str, Any], now: datetime) -> date | None:
    """The day IBKR hasn't published, while it is too soon to ask for it again."""
    awaiting = state.get("awaiting") or {}
    try:
        day = date.fromisoformat(awaiting["day"])
        asked_at = datetime.fromisoformat(awaiting["asked_at"])
    except (KeyError, TypeError, ValueError):
        return None
    return day if now - asked_at < NOT_PUBLISHED_RETRY else None


def _from_older_version(state: dict[str, Any]) -> dict[str, Any]:
    """Start over from a 0.1.x state: those records were written through the API, not synced.

    The synced records start empty, so the first statement covers the whole
    year again and opens what was held before it.
    """
    if "openings" in state or "last_report_end" not in state:
        return state
    return {}


class IbkrFlex:
    def __init__(
        self,
        *,
        http: flex_fetch.Http | None = None,
        today: Callable[[], date] | None = None,
        now: Callable[[], datetime] | None = None,
        fetch_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._http = http
        # One clock (UTC). A test that only names the day gets its midday.
        if now is None:
            now = (
                (lambda: datetime.combine(today(), time(12), tzinfo=timezone.utc))
                if today is not None
                else (lambda: datetime.now(timezone.utc))
            )
        self._now = now
        self._today = today or (lambda: now().date())
        self._fetch_kwargs = fetch_kwargs or {}

    def _fetch(self, token: str, query_id: str, start: date, end: date, pending: flex_fetch.Pending | None) -> flex_fetch.Outcome:
        kwargs = dict(self._fetch_kwargs)
        if self._http is not None:
            kwargs["http"] = self._http
        return flex_fetch.fetch(token=token, query_id=query_id, start=start, end=end, pending=pending, **kwargs)

    def sync(self, params: dict[str, Any], context: Any) -> dict[str, Any]:
        token, query_id = _credentials(context)
        state = _from_older_version(dict(params.get("state") or {}))
        pending = flex_fetch.Pending.from_state(state.get("flex"))
        now = self._now()
        window = report_window(state, self._today(), now)
        if pending is not None:
            # The statement IBKR is generating covers the days it was asked
            # for. Today's window may already end a day later: that day waits
            # for the next statement instead of being recorded as imported.
            start, end = pending.from_date, pending.to_date
        elif window is not None:
            start, end = window
        else:
            return {"state": state, "data": None}
        try:
            try:
                outcome = self._fetch(token, query_id, start, end, pending)
            except FlexNotAvailable:
                # IBKR hasn't published the newest day yet (D25): leave it
                # for a few hours and take the days before it, if any.
                state.pop("flex", None)
                state["awaiting"] = {"day": end.isoformat(), "asked_at": now.isoformat()}
                end -= timedelta(days=1)
                if start > end:
                    return {"state": state, "data": None}
                outcome = self._fetch(token, query_id, start, end, None)
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
        if (state.get("awaiting") or {}).get("day", "") <= state["last_report_end"]:
            state.pop("awaiting", None)
        return {"state": state, "data": data}


plugin = IbkrFlex()
