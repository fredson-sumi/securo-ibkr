"""Securo calls `plugin.sync(params, context)` in its plugin runner.

- `context.secrets["token"]`: the Flex Web Service token
- `context.config["query_id"]`: the Activity Flex Query id
- `context.securo`: `api_url`, `workspace_id`, `connection_id` and a `token`
  limited to the manifest's `api_scopes`

`params["state"]` is what the last sync returned (`{}` the first time).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from securo_ibkr.errors import NeedsAuth

_QUERY_ID = re.compile(r"^[0-9]{1,32}$")


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


class IbkrFlex:
    def sync(self, params: dict[str, Any], context: Any) -> dict[str, Any]:
        _credentials(context)
        state = dict(params.get("state") or {})
        state["checked_at"] = datetime.now(timezone.utc).isoformat()
        return {"state": state}


plugin = IbkrFlex()
