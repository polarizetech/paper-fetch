"""scite: search through its MCP server, with the subscriber's own sign-in.

scite indexes 210M+ papers with the full text of citing sentences, so it finds work the
metadata-only indexes rank poorly, and each hit says how often it has been supported or contrasted.
Its REST search (`/api_partner/search`) needs a partner token; its MCP server needs only the
subscriber's sign-in (see `scite_auth`). This provider is a small MCP client for that server: it
initialises a session over streamable HTTP and calls one tool, `search_literature`.

## What a scite hit is, and is not

- **Search only, never a copy source.** `can_locate` is False. scite's access links route to
  publishers and a document-delivery service, and they carry the account's email address; none of
  it is kept. A hit's DOI goes through `fetch()`, and the other providers decide openness.
- **`is_oa` is scite's flag** (`isOa`), with its `oaStatus` in `extra`. With `oa_only`, hits scite
  marks closed are dropped here; the tool has no open-access filter, so up to three times `limit`
  is asked for and filtered.
- **`extra` carries the tally** (supporting / contrasting / mentioning / citing publications) and
  any editorial notices (retraction, correction, concern). Abstracts and citation statements are
  not kept.
- **Not cached** (`cacheable = False`): scite's terms on storing its results in a shared store
  have not been checked.

## Metered, so opt-in

The subscription allows **250 MCP calls a month** (measured 2026-10-06: a day in the default
search set, behind a gateway and several sessions, spent it). So `scite` is not in the default
search set and `metered` is True: a search asks it only when told to (`also=["scite"]`, `--scite`,
or by naming it), and then once, with the query as written, never with a profile's variants. Past
the limit the tool answers with an error, reported `unavailable`.

## Failure, of two kinds

Not signed in is `skipped` (`available()` is False, no network). A 401 gets one token refresh and
one retry; a 404 on a session gets one new session. Beyond that:

- **An outside problem is `ProviderUnavailable`**, and the search carries on with the other
  providers: an unreachable server, a 5xx, a 429, a refused or expired sign-in, and a tool error
  that says so in words. **A spent allowance** ("monthly MCP usage limit") pauses scite for the
  rest of the process, so it is not asked again by every search; the others pause briefly.
- **A defect is `ProviderBroken`**, logged and named in the search's `broken` list: an answer
  that is not JSON-RPC, a 4xx or a JSON-RPC error on our request, a tool error of no recognised
  kind, or an answer without a `hits` list (a changed format must not read as "no results").
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

from .. import scite_auth
from ..http import HttpClient, Response
from .base import (
    PAUSE_QUOTA_S,
    PAUSE_RATE_LIMIT_S,
    Hit,
    Provider,
    ProviderBroken,
    ProviderUnavailable,
    clean_doi,
)

__all__ = ["Scite"]

_PROTOCOL = "2025-06-18"
# A tool error says in words which kind it is. A spent allowance will not come back in this
# process; a busy or failing service will; anything else is treated as a defect and flagged.
_SPENT = re.compile(r"usage limit|monthly|quota|pay-as-you-go|allowance|subscription", re.I)
_PASSING = re.compile(r"busy|overload|unavailable|timed? ?out|temporar|try again|rate.?limit", re.I)
_TALLY = ("total", "supporting", "contrasting", "mentioning", "citingPublications")


def _year(x: dict[str, Any]) -> int | None:
    y = x.get("year") or str(x.get("date") or "")[:4]
    try:
        return int(y)
    except (TypeError, ValueError):
        return None


def _message(r: Response, want_id: int) -> dict[str, Any]:
    """The JSON-RPC answer to request `want_id`, from a JSON body or an event stream."""
    if "text/event-stream" in r.headers.get("content-type", ""):
        text = r.body.decode("utf-8", "replace").replace("\r\n", "\n")
        for event in text.split("\n\n"):
            data = "\n".join(ln[5:].lstrip() for ln in event.split("\n") if ln.startswith("data:"))
            if not data:
                continue
            try:
                msg = json.loads(data)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("id") == want_id:
                return msg
        raise ProviderBroken("scite answered with a stream that held no result")
    try:
        msg = json.loads(r.body)
    except ValueError as e:
        raise ProviderBroken("scite answered with something that is not JSON") from e
    if not isinstance(msg, dict):
        raise ProviderBroken("scite answered with something that is not a JSON-RPC message")
    return msg


class Scite(Provider):
    name, label = "scite", "scite (Smart Citations search, via its MCP server)"
    can_search, can_locate = True, False
    cacheable = False
    metered = True
    min_interval_s = 1.5
    timeout_s = 45.0
    terms = (
        "Subscription, 250 MCP calls a month; opt-in per search. Sign in once with `paper-fetch "
        "scite-login` (OAuth, no API key). Search only: hits carry scite's open-access flag and "
        "citation tallies; copies come through the other providers. Not cached."
    )
    MCP = "https://api.scite.ai/mcp"
    MAX = 50  # hits carry citation lists; scite asks clients to keep `limit` small

    def __init__(self, http: HttpClient) -> None:
        super().__init__(http)
        self._session: str | None = None
        self._protocol: str | None = None
        self._next_id = 0
        self._mcp = threading.RLock()  # one session, used by one thread at a time

    def available(self) -> tuple[bool, str]:
        if scite_auth.signed_in():
            return True, ""
        return False, "not signed in: run `paper-fetch scite-login`"

    # -- transport ------------------------------------------------------------------------------

    def _post(self, payload: dict[str, Any], token: str) -> Response:
        wait = self.min_interval_s - (time.monotonic() - self._last)
        if wait > 0 and payload.get("method") == "tools/call":
            time.sleep(wait)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        }
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        if self._protocol:
            headers["MCP-Protocol-Version"] = self._protocol
        self._last = time.monotonic()
        self.calls += 1
        try:
            return self.http.post(
                self.MCP, json.dumps(payload).encode(), headers, timeout=self.timeout_s
            )
        except ConnectionError as e:
            raise ProviderUnavailable(f"scite unreachable: {str(e)[-120:]}") from e

    def _token(self, *, renew: bool = False) -> str:
        try:
            return scite_auth.refresh(self.http) if renew else scite_auth.access_token(self.http)
        except scite_auth.SciteAuthError as e:
            raise ProviderUnavailable(str(e)) from e
        except ConnectionError as e:
            raise ProviderUnavailable(f"scite sign-in unreachable: {str(e)[-120:]}") from e

    def _send(self, payload: dict[str, Any]) -> Response:
        """POST one message; a 401 gets one token refresh and one retry."""
        r = self._post(payload, self._token())
        if r.status == 401:
            r = self._post(payload, self._token(renew=True))
            if r.status == 401:
                raise ProviderUnavailable(
                    "scite refused the sign-in: run `paper-fetch scite-login`"
                )
        if r.status == 429:
            raise ProviderUnavailable(
                "scite rate-limited us (HTTP 429)", pause_s=PAUSE_RATE_LIMIT_S
            )
        return r

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        rid = self._next_id
        r = self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if r.status == 404 and self._session and method != "initialize":
            self._session = self._protocol = None  # the server forgot the session: start one
            self._open()
            return self._request(method, params)
        if r.status >= 500 or r.status in (402, 403):
            raise ProviderUnavailable(f"scite HTTP {r.status} on {method}")
        if r.status != 200:  # it rejected what we sent: a defect, not an outage
            detail = r.body[:160].decode("utf-8", "replace").strip()
            raise ProviderBroken(f"scite rejected {method} (HTTP {r.status}): {detail}")
        msg = _message(r, rid)
        if msg.get("error"):  # a JSON-RPC error is about the request, so it is ours to fix
            why = (msg["error"] or {}).get("message") or "error"
            raise ProviderBroken(f"scite rejected {method}: {str(why)[:160]}")
        if method == "initialize":
            self._session = r.headers.get("mcp-session-id")
        result = msg.get("result")
        return result if isinstance(result, dict) else {}

    def _open(self) -> None:
        from .. import __version__  # noqa: PLC0415 -- avoids an import cycle with the package root

        res = self._request(
            "initialize",
            {
                "protocolVersion": _PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "paper-fetch", "version": __version__},
            },
        )
        self._protocol = str(res.get("protocolVersion") or _PROTOCOL)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        with self._mcp:
            if self._protocol is None:
                self._open()
            res = self._request("tools/call", {"name": tool, "arguments": arguments})
        text = next(
            (c.get("text") for c in res.get("content") or [] if c.get("type") == "text"), None
        )
        if res.get("isError"):
            said = str(text or "tool error")
            if _SPENT.search(said):
                raise ProviderUnavailable(f"scite {tool}: {said[:160]}", pause_s=PAUSE_QUOTA_S)
            if _PASSING.search(said):
                raise ProviderUnavailable(f"scite {tool}: {said[:160]}")
            raise ProviderBroken(f"scite {tool} failed: {said[:200]}")
        data = res.get("structuredContent")
        if not isinstance(data, dict) and text:
            try:
                data = json.loads(text)
            except ValueError as e:
                raise ProviderBroken(f"scite {tool} returned text, not JSON") from e
        if isinstance(data, dict) and "hits" not in data and isinstance(data.get("result"), dict):
            data = data["result"]
        if not isinstance(data, dict) or not isinstance(data.get("hits"), list):
            keys = sorted(data) if isinstance(data, dict) else type(data).__name__
            raise ProviderBroken(f"scite {tool} answered without a `hits` list: {keys}")
        return data

    # -- search ---------------------------------------------------------------------------------

    def search(self, query: str, *, oa_only: bool = True, limit: int = 10) -> list[Hit]:
        ok, why = self.available()
        if not ok:
            raise ProviderUnavailable(f"scite {why}")
        ask = min(limit * 3 if oa_only else limit, self.MAX)
        data = self._call(
            "search_literature", {"term": query, "limit": ask, "user_intent": "paper_search"}
        )
        out: list[Hit] = []
        for x in data.get("hits") or []:
            if oa_only and not x.get("isOa"):
                continue
            tally = x.get("tally") or {}
            extra: dict[str, Any] = {
                "journal": x.get("journal"),
                "oa_status": x.get("oaStatus"),
                "tally": {k: tally[k] for k in _TALLY if isinstance(tally.get(k), int)},
            }
            notices = [
                {k: n[k] for k in ("type", "status", "noticeDoi", "date") if n.get(k)}
                for n in x.get("editorialNotices") or []
                if isinstance(n, dict)
            ]
            if notices:
                extra["editorial_notices"] = notices
            out.append(
                Hit(
                    self.name,
                    x.get("title"),
                    _year(x),
                    {"doi": clean_doi(x.get("doi"))},
                    bool(x["isOa"]) if isinstance(x.get("isOa"), bool) else None,
                    [a.get("authorName") for a in (x.get("authors") or [])[:3]],
                    [],
                    extra,
                )
            )
            if len(out) >= limit:
                break
        return out
