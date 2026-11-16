"""The resumable Flex fetch. All responses are fakes; no network."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from securo_ibkr import fetch
from securo_ibkr.errors import FlexTransient, NeedsAuth
from securo_ibkr.plugin import IbkrFlex, report_window
from tests.fake_report import report_xml

TOKEN = "fake-token-123456"
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
GET = "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.GetStatement"


def send_ok(ref: str = "REF1", url: str = GET) -> tuple[int, bytes]:
    return 200, (
        f"<FlexStatementResponse><Status>Success</Status><ReferenceCode>{ref}</ReferenceCode>"
        f"<Url>{url}</Url></FlexStatementResponse>"
    ).encode()


def flex_error(code: str) -> tuple[int, bytes]:
    return 200, (
        f"<FlexStatementResponse><Status>Fail</Status><ErrorCode>{code}</ErrorCode>"
        f"<ErrorMessage>secret {TOKEN} echoed</ErrorMessage></FlexStatementResponse>"
    ).encode()


GENERATING = flex_error("1019")
READY = (200, report_xml().encode())


class FakeIbkr:
    """Answers each call from a script; records URLs and params."""

    def __init__(self, *responses: tuple[int, bytes] | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, params: dict[str, str]) -> tuple[int, bytes]:
        self.calls.append((url, dict(params)))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def sends(self) -> int:
        return sum(1 for url, _ in self.calls if url == fetch.SEND_REQUEST_URL)


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def run(http: FakeIbkr, pending: fetch.Pending | None = None, now: datetime = NOW) -> fetch.Outcome:
    clock = FakeClock()
    return fetch.fetch(
        token=TOKEN, query_id="123", start=date(2025, 11, 16), end=date(2026, 11, 15),
        pending=pending, http=http, sleep=clock.sleep, clock=clock, now=lambda: now,
    )


def test_ready_within_budget_returns_report_in_one_call():
    http = FakeIbkr(send_ok(), GENERATING, READY)
    outcome = run(http)
    assert outcome.report is not None and outcome.pending is None
    assert outcome.report.account_id == "U1234567"
    assert http.sends == 1
    assert http.calls[0][1]["fd"] == "20251116" and http.calls[0][1]["td"] == "20261115"
    assert http.calls[1] == (GET, {"t": TOKEN, "q": "REF1", "v": "3"})


def test_slow_report_is_handed_back_and_resumed_without_a_new_request():
    polls = fetch.POLL_BUDGET // fetch.POLL_INTERVAL
    first = FakeIbkr(send_ok(), *[GENERATING] * polls)
    outcome = run(first)
    assert outcome.report is None and outcome.pending is not None
    assert outcome.pending.reference == "REF1"
    assert len(first.calls) == 1 + polls  # never more than ~6 requests per call

    restored = fetch.Pending.from_state(outcome.pending.to_state())
    second = FakeIbkr(READY)
    resumed = run(second, pending=restored)
    assert resumed.report is not None
    assert second.sends == 0


def test_expired_reference_starts_over():
    old = fetch.Pending("OLD", GET, date(2025, 11, 16), date(2026, 11, 15), NOW - timedelta(hours=7))
    http = FakeIbkr(send_ok("NEW"), READY)
    assert run(http, pending=old).report is not None
    assert http.sends == 1 and http.calls[1][1]["q"] == "NEW"


def test_reference_ibkr_no_longer_knows_is_replaced_in_the_same_call():
    old = fetch.Pending("GONE", GET, date(2025, 11, 16), date(2026, 11, 15), NOW - timedelta(minutes=30))
    http = FakeIbkr(flex_error("1017"), send_ok("NEW"), READY)  # 1017: reference code invalid
    assert run(http, pending=old).report is not None
    assert [p["q"] for _, p in http.calls if "q" in p] == ["GONE", "123", "NEW"]


@pytest.mark.parametrize("response", [(429, b""), (503, b""), flex_error("1018"), OSError("reset")])
def test_throttling_and_hiccups_while_polling_keep_waiting(response):
    http = FakeIbkr(send_ok(), response, READY)
    assert run(http).report is not None


def test_rate_limited_send_request_is_pending_not_a_failure():
    plugin = IbkrFlex(http=FakeIbkr(flex_error("1018")), today=lambda: date(2026, 11, 16))
    result = plugin.sync({"state": {}}, _context())
    assert "flex" not in result["state"]


@pytest.mark.parametrize("code", ["1012", "1015", "1020"])
def test_credential_errors_need_auth_and_never_echo_the_token(code):
    with pytest.raises(NeedsAuth) as exc:
        run(FakeIbkr(flex_error(code)))
    assert TOKEN not in str(exc.value)


def test_http_403_needs_auth():
    with pytest.raises(NeedsAuth):
        run(FakeIbkr(send_ok(), (403, b"")))


def test_csv_output_needs_auth():
    with pytest.raises(NeedsAuth) as exc:
        run(FakeIbkr(send_ok(), (200, b"ClientAccountID,Currency\nU1,USD\n")))
    assert exc.value.code == "flex_query_output_format"


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/GetStatement",
        "http://gdcdyn.interactivebrokers.com/GetStatement",
        "https://user:pw@gdcdyn.interactivebrokers.com/GetStatement",
        "https://gdcdyn.interactivebrokers.com/GetStatement?x=1",
        "https://gdcdyn.interactivebrokers.com:8443/GetStatement",
    ],
)
def test_untrusted_statement_url_is_refused(url):
    with pytest.raises(FlexTransient) as exc:
        run(FakeIbkr(send_ok(url=url)))
    assert "evil" not in str(exc.value)


def test_transport_failure_on_send_is_transient_without_details():
    with pytest.raises(FlexTransient) as exc:
        run(FakeIbkr(OSError(f"https://x/?t={TOKEN}")))
    assert TOKEN not in str(exc.value)


# --- sync() and the report window -------------------------------------------------


def _context(token: str = TOKEN, query_id: str = "123"):
    return SimpleNamespace(secrets={"token": token}, config={"query_id": query_id}, securo={})


def test_window_is_a_year_first_then_continues_from_the_last_import():
    today = date(2026, 11, 16)
    assert report_window({}, today) == (date(2025, 11, 16), date(2026, 11, 15))
    assert report_window({"last_report_end": "2026-11-10"}, today) == (date(2026, 11, 11), date(2026, 11, 15))
    assert report_window({"last_report_end": "2026-11-15"}, today) is None
    assert report_window({"last_report_end": "2020-01-01"}, today)[0] == date(2025, 11, 16)


def test_sync_keeps_pending_request_in_state_and_finishes_next_time():
    polls = fetch.POLL_BUDGET // fetch.POLL_INTERVAL
    clock = FakeClock()
    kwargs = {"sleep": clock.sleep, "clock": clock}
    first = IbkrFlex(http=FakeIbkr(send_ok(), *[GENERATING] * polls), today=lambda: date(2026, 11, 16), fetch_kwargs=kwargs)
    state = first.sync({"state": {}}, _context())["state"]
    assert state["flex"]["reference"] == "REF1"

    second = IbkrFlex(http=FakeIbkr(READY), today=lambda: date(2026, 11, 16), fetch_kwargs=kwargs)
    result = second.sync({"state": state}, _context())
    state, data = result["state"], result["data"]
    assert "flex" not in state
    assert state["last_report_end"] == "2026-11-15"
    assert data["contract"] == 1
    assert [a["external_id"] for a in data["accounts"]] == ["U1234567:EUR", "U1234567:USD"]
    assert len(data["holdings"]) == 2


def test_while_ibkr_generates_there_is_no_data():
    polls = fetch.POLL_BUDGET // fetch.POLL_INTERVAL
    clock = FakeClock()
    plugin = IbkrFlex(http=FakeIbkr(send_ok(), *[GENERATING] * polls), today=lambda: date(2026, 11, 16),
                      fetch_kwargs={"sleep": clock.sleep, "clock": clock})
    result = plugin.sync({"state": {}}, _context())
    assert result["data"] is None and "last_report_end" not in result["state"]


def test_sync_does_nothing_when_yesterday_is_already_imported():
    http = FakeIbkr()
    plugin = IbkrFlex(http=http, today=lambda: date(2026, 11, 16))
    state = {"last_report_end": "2026-11-15", "openings": {}}
    result = plugin.sync({"state": state}, _context())
    assert result == {"state": state, "data": None}
    assert http.calls == []


@pytest.mark.parametrize("token,query_id", [("", "123"), ("x", "12a"), ("x", "")])
def test_sync_refuses_bad_inputs_with_needs_auth(token, query_id):
    with pytest.raises(NeedsAuth):
        IbkrFlex(http=FakeIbkr()).sync({"state": {}}, _context(token, query_id))


def test_a_state_from_0_1_starts_over_with_a_full_year():
    old = {"last_report_end": "2026-11-10", "accounts": {"U1234567:USD": {"id": "x"}}, "assets": {}, "last_sync": {}}
    clock = FakeClock()
    plugin = IbkrFlex(http=FakeIbkr(send_ok(), READY), today=lambda: date(2026, 11, 16),
                      fetch_kwargs={"sleep": clock.sleep, "clock": clock})
    result = plugin.sync({"state": old}, _context())
    assert set(result["state"]) == {"openings", "last_report_end"}
    assert result["state"]["openings"]  # the year's first statement opened what was held


def test_a_state_from_0_2_continues():
    state = {"last_report_end": "2026-11-10", "openings": {}}
    clock = FakeClock()
    plugin = IbkrFlex(http=FakeIbkr(send_ok(), READY), today=lambda: date(2026, 11, 16),
                      fetch_kwargs={"sleep": clock.sleep, "clock": clock})
    result = plugin.sync({"state": state}, _context())
    assert result["state"]["openings"] == {}

