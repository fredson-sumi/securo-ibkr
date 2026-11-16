"""Getting an Activity Flex statement from IBKR within one plugin call.

IBKR generates a statement asynchronously: `SendRequest` returns a reference
code, and `GetStatement` returns the report once it is ready, which for a
year of activity can take minutes. A plugin call is killed after 60 seconds,
so a request that isn't ready within `POLL_BUDGET` seconds is handed back as
`pending` and kept in the connection's state; the next sync polls the same
reference instead of asking IBKR to generate the report again.

The token travels only in query strings to IBKR's two Flex hosts. No URL,
response body or upstream error text ever goes into an exception message.
"""
from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from securo_ibkr.errors import FlexPending, FlexTransient, NeedsAuth
from securo_ibkr.flex import (
    MAX_RESPONSE_BYTES,
    POLL_RETRY_ERRORS,
    ParsedReport,
    child_text,
    detect_non_xml,
    local_name,
    parse_element,
    parse_report,
    raise_for_flex_error,
)

SEND_REQUEST_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
DEFAULT_STATEMENT_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"
#: The hosts declared in the manifest's `network`; the egress refuses any other.
ALLOWED_HOSTS = frozenset({"ndcdyn.interactivebrokers.com", "gdcdyn.interactivebrokers.com"})
HTTP_TIMEOUT = 20
#: Seconds of polling per call, leaving room in the 60 s limit for the rest of the sync.
POLL_BUDGET = 40
POLL_INTERVAL = 8
#: How long a reference is polled across syncs before a fresh request replaces it.
REFERENCE_TTL = timedelta(hours=6)
_USER_AGENT = "securo-ibkr-plugin"

#: (status, body) for a GET of `url` with `params`. Raises `OSError` on transport failure.
Http = Callable[[str, dict[str, str]], tuple[int, bytes]]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def urllib_get(url: str, params: dict[str, str]) -> tuple[int, bytes]:
    """GET through the runner's HTTPS_PROXY (urllib reads it from the environment)."""
    request = urllib.request.Request(
        f"{url}?{urllib.parse.urlencode(params)}",
        headers={"Accept": "application/xml", "User-Agent": _USER_AGENT},
    )
    try:
        with _opener.open(request, timeout=HTTP_TIMEOUT) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            status = response.status
    except urllib.error.HTTPError as exc:
        status, body = exc.code, b""
        exc.close()
    if len(body) > MAX_RESPONSE_BYTES:
        raise FlexTransient("IBKR returned a statement larger than 20 MB.")
    return status, body


def statement_url(url: str | None) -> str:
    """The download URL IBKR returned, if it is a plain https URL on a declared host."""
    if not url:
        return DEFAULT_STATEMENT_URL
    parts = urllib.parse.urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.port not in (None, 443)
        or (parts.hostname or "").lower() not in ALLOWED_HOSTS
    ):
        raise FlexTransient("IBKR returned a statement URL on an unexpected host.")
    return url


@dataclass
class Pending:
    """A statement IBKR is still generating, kept in `state["flex"]`."""

    reference: str
    url: str
    from_date: date
    to_date: date
    requested_at: datetime

    def to_state(self) -> dict[str, str]:
        return {
            "reference": self.reference,
            "url": self.url,
            "from": self.from_date.isoformat(),
            "to": self.to_date.isoformat(),
            "requested_at": self.requested_at.isoformat(),
        }

    @classmethod
    def from_state(cls, data: dict[str, Any] | None) -> Pending | None:
        if not data:
            return None
        try:
            return cls(
                reference=str(data["reference"]),
                url=statement_url(data.get("url")),
                from_date=date.fromisoformat(data["from"]),
                to_date=date.fromisoformat(data["to"]),
                requested_at=datetime.fromisoformat(data["requested_at"]),
            )
        except (KeyError, TypeError, ValueError, FlexTransient):
            return None  # unreadable: start over


@dataclass
class Outcome:
    """Either a parsed report, or the request to keep polling next time."""

    report: ParsedReport | None = None
    pending: Pending | None = None


def _check_status(status: int) -> None:
    if status in (401, 403):
        raise NeedsAuth("IBKR rejected the Flex token or this server's IP address.")
    if status == 429:
        raise FlexPending("IBKR's Flex rate limit was reached.")


def send_request(http: Http, token: str, query_id: str, start: date, end: date, now: datetime) -> Pending:
    try:
        status, body = http(
            SEND_REQUEST_URL,
            {"t": token, "q": query_id, "v": "3", "fd": start.strftime("%Y%m%d"), "td": end.strftime("%Y%m%d")},
        )
    except OSError as exc:
        raise FlexTransient(f"Couldn't reach IBKR's Flex Web Service ({type(exc).__name__}).") from None
    _check_status(status)
    if status != 200:
        raise FlexTransient(f"IBKR's Flex Web Service answered HTTP {status}.")
    try:
        envelope = parse_element(body)
    except Exception:
        raise FlexTransient("IBKR returned an unreadable response.") from None
    if (child_text(envelope, "Status") or "").lower() != "success":
        raise_for_flex_error(child_text(envelope, "ErrorCode"))
    reference = child_text(envelope, "ReferenceCode")
    if not reference:
        raise FlexTransient("IBKR didn't return a reference code for the statement.")
    return Pending(reference, statement_url(child_text(envelope, "Url", "url")), start, end, now)


def _poll_once(http: Http, token: str, pending: Pending) -> ParsedReport | None:
    """The report, or None while IBKR is still generating it."""
    try:
        status, body = http(pending.url, {"t": token, "q": pending.reference, "v": "3"})
    except OSError:
        return None  # transient; keep polling
    if status in (401, 403):
        _check_status(status)
    if status == 429 or status in (408, 425) or status >= 500:
        return None
    if status != 200:
        raise FlexTransient(f"IBKR's statement download answered HTTP {status}.")
    detect_non_xml(body)
    try:
        root = parse_element(body)
    except Exception:
        raise FlexTransient("IBKR returned an unreadable statement.") from None
    if local_name(root.tag) in {"FlexQueryResponse", "FlexStatements"}:
        return parse_report(body)
    code = child_text(root, "ErrorCode")
    if code in POLL_RETRY_ERRORS:
        return None
    raise_for_flex_error(code)


def fetch(
    *,
    token: str,
    query_id: str,
    start: date,
    end: date,
    pending: Pending | None,
    http: Http = urllib_get,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> Outcome:
    """Poll `pending` (or a new request for `start`..`end`) for up to `POLL_BUDGET` seconds."""
    deadline = clock() + POLL_BUDGET
    if pending is not None and now() - pending.requested_at > REFERENCE_TTL:
        pending = None
    if pending is not None:
        # A carried-over reference is polled at once. If IBKR no longer knows
        # it, start over in this call: a failed sync keeps no state, so the
        # broken reference would otherwise come back every time.
        try:
            report = _poll_once(http, token, pending)
        except FlexTransient:
            pending = None
        else:
            if report is not None:
                return Outcome(report=report)
    if pending is None:
        pending = send_request(http, token, query_id, start, end, now())
    while clock() + POLL_INTERVAL <= deadline:
        sleep(POLL_INTERVAL)  # IBKR needs a moment after SendRequest, and asks for spacing
        report = _poll_once(http, token, pending)
        if report is not None:
            return Outcome(report=report)
    return Outcome(pending=pending)
