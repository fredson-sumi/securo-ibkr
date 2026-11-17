"""How a sync reports trouble to Securo.

Securo treats an exception named `NeedsAuth` as "the person has to fix the
connection" and any other exception as a failure it retries with backoff.
`FlexPending` is neither: the statement just isn't ready (still generating,
or IBKR's rate limit), so `sync` catches it and ends successfully, keeping
the request in `state` for the next run.
"""
from __future__ import annotations

HELP_URL = "https://www.ibkrguides.com/brokerportal/performanceandstatements/flex3.htm"


class NeedsAuth(Exception):
    """The token, query or IBKR settings must be fixed by the person."""

    def __init__(self, message: str, *, code: str = "credentials_invalid") -> None:
        super().__init__(f"{message} Help: {HELP_URL}")
        self.code = code


class FlexPending(Exception):
    """The statement isn't ready yet; try again on the next sync."""


class FlexTransient(Exception):
    """A temporary IBKR or network failure; Securo retries with backoff."""


class FlexNotAvailable(FlexTransient):
    """IBKR has no statement for the days asked (error 1003).

    For a statement that ends yesterday it means IBKR hasn't published that
    day yet, which `sync` waits out; anywhere else it is a failure like any
    other.
    """
