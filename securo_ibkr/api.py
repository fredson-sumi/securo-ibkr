"""A small client for the Securo API, with the connection's scoped token.

The token covers only the manifest's `api_scopes`. Requests go straight to
the Securo backend (the runner sets NO_PROXY for it). Error messages carry the
method, path and status, never the token or a response body.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

HTTP_TIMEOUT = 15

#: (method, url, headers, body) -> (status, body)
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


class ApiError(Exception):
    def __init__(self, method: str, path: str, status: int, detail: str = "") -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"Securo API {method} {path} answered HTTP {status}.")


class NotFound(ApiError):
    pass


class OutOfTime(Exception):
    """The call's time budget is spent; the work left resumes next sync."""


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read()
        finally:
            exc.close()


class SecuroApi:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: Transport = urllib_transport,
        deadline: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._transport = transport
        self._deadline = deadline
        self._clock = clock

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        if self._deadline is not None and self._clock() >= self._deadline:
            raise OutOfTime()
        url = self._base + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        try:
            status, raw = self._transport(method, url, headers, data)
        except OSError as exc:
            raise ApiError(method, path, 0, type(exc).__name__) from None
        if status == 404:
            raise NotFound(method, path, status)
        if status >= 400:
            raise ApiError(method, path, status, _detail(raw))
        if not raw:
            return None
        return json.loads(raw)

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params or None)

    def post(self, path: str, body: dict[str, Any]) -> Any:
        return self.request("POST", path, body=body)

    def patch(self, path: str, body: dict[str, Any]) -> Any:
        return self.request("PATCH", path, body=body)

    def delete(self, path: str) -> Any:
        return self.request("DELETE", path)


def _detail(raw: bytes) -> str:
    """A short, safe summary of a validation error, for logs and tests."""
    try:
        detail = json.loads(raw).get("detail")
    except Exception:
        return ""
    return detail[:200] if isinstance(detail, str) else ""
