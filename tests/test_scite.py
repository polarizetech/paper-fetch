"""scite: the sign-in, the MCP session, and what a hit keeps. A fake server stands in for scite."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import stat
import threading
import time
import urllib.error
import urllib.parse
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from conftest import make_library
from paper_fetch import MemoryStore, cli, scite_auth
from paper_fetch.http import Http, Response
from paper_fetch.providers import DEFAULT_SEARCH, REGISTRY, ProviderUnavailable, Scite

META = {
    "issuer": "https://api.scite.ai",
    "authorization_endpoint": "https://api.scite.ai/mcp/oauth/authorize",
    "token_endpoint": "https://api.scite.ai/mcp/oauth/token",
    "registration_endpoint": "https://api.scite.ai/mcp/oauth/register",
}

# Shaped like a real `search_literature` answer, including the fields that must NOT be kept: the
# access links carry the account's email address.
HITS = [
    {
        "doi": "10.5555/OPEN.1",
        "title": "An open paper",
        "authors": [{"authorName": f"Author {i}"} for i in range(5)],
        "journal": "journal of examples",
        "abstract": "An abstract that is not kept.",
        "year": 2013,
        "date": "2013-12-15",
        "tally": {
            "total": 9,
            "supporting": 3,
            "contrasting": 1,
            "mentioning": 5,
            "citingPublications": 7,
        },
        "citations": [
            {"sourceDoi": "10.5555/open.1", "targetDoi": "10.5555/x", "type": "mentioning"}
        ],
        "editorialNotices": [
            {"status": "retracted", "noticeDoi": "10.5555/notice", "date": "2020"}
        ],
        "isOa": True,
        "oaStatus": "gold",
        "access": {"url": "https://doi.org/10.5555/open.1", "accessType": "open"},
        "url": "https://doi.org/10.5555/open.1",
    },
    {
        "doi": "10.5555/closed.2",
        "title": "A closed paper",
        "authors": [{"authorName": "Solo Author"}],
        "date": "2001-01-01",
        "tally": {"total": 0},
        "isOa": False,
        "oaStatus": "closed",
        "access": {"url": "https://delivery.example/po?email=someone%40example.org"},
        "url": "https://delivery.example/po?email=someone%40example.org",
    },
]


def J(obj: Any, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    h = {"content-type": "application/json", **(headers or {})}
    return Response(status, h, json.dumps(obj).encode(), "https://api.scite.ai/x")


class FakeScite:
    """scite's sign-in endpoints and its MCP endpoint, as one HttpClient."""

    def __init__(self) -> None:
        self.token = "access-1"  # the access token the MCP endpoint accepts
        self.refresh_ok = True
        self.rotate = True
        self.sse = False
        self.tool_error = False
        self.status_for_call: int | None = None
        self.forget_session_once = False
        self.calls = 0
        self.rpc: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.forms: list[dict[str, str]] = []
        self.registered: dict[str, Any] = {}
        self.refreshes = 0

    def get(self, url: str, headers: dict[str, str] | None = None, **_k: Any) -> Response:
        self.calls += 1
        if url.endswith("/.well-known/oauth-authorization-server"):
            return J(META)
        return Response(404, {}, b"", url)

    def post(
        self, url: str, body: bytes, headers: dict[str, str] | None = None, **_k: Any
    ) -> Response:
        self.calls += 1
        if url == META["registration_endpoint"]:
            self.registered = json.loads(body)
            return J({"client_id": "client-xyz"}, 201)
        if url == META["token_endpoint"]:
            form = {k: v[0] for k, v in urllib.parse.parse_qs(body.decode()).items()}
            self.forms.append(form)
            if form["grant_type"] == "refresh_token":
                self.refreshes += 1
                if not self.refresh_ok:
                    return J({"error": "invalid_grant"}, 400)
                self.token = f"access-{self.refreshes + 1}"
                out: dict[str, Any] = {"access_token": self.token, "expires_in": 3600}
                if self.rotate:
                    out["refresh_token"] = f"refresh-{self.refreshes + 1}"
                return J(out)
            return J({"access_token": self.token, "refresh_token": "refresh-1", "expires_in": 3600})
        assert url == Scite.MCP
        msg = json.loads(body)
        h = dict(headers or {})
        self.rpc.append(msg)
        self.headers.append(h)
        if h.get("Authorization") != f"Bearer {self.token}":
            return J({"detail": "Unauthorized"}, 401)
        method = msg["method"]
        if method == "initialize":
            return self._answer(
                msg, {"protocolVersion": "2025-06-18"}, {"mcp-session-id": "sess-1"}
            )
        if method == "notifications/initialized":
            return Response(202, {}, b"", url)
        if self.forget_session_once:
            self.forget_session_once = False
            return J({"detail": "session not found"}, 404)
        if self.status_for_call:
            return J({"detail": "no"}, self.status_for_call)
        text = "scite is busy" if self.tool_error else json.dumps({"total": 2, "hits": HITS})
        return self._answer(
            msg, {"content": [{"type": "text", "text": text}], "isError": self.tool_error}
        )

    def _answer(
        self, msg: dict[str, Any], result: dict[str, Any], headers: dict[str, str] | None = None
    ) -> Response:
        reply = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        if not self.sse:
            return J(reply, headers=headers)
        other = {"jsonrpc": "2.0", "method": "notifications/message", "params": {}}
        body = f"event: message\r\ndata: {json.dumps(other)}\r\n\r\n"
        body += f"event: message\r\ndata: {json.dumps(reply)}\r\n\r\n"
        return Response(
            200, {"content-type": "text/event-stream", **(headers or {})}, body.encode(), Scite.MCP
        )


def sign_in(
    access: str = "access-1", *, expires_in: float = 3600, refresh: str = "refresh-1"
) -> None:
    scite_auth._save(
        {
            "client_id": "client-xyz",
            "token_endpoint": META["token_endpoint"],
            "access_token": access,
            "refresh_token": refresh,
            "expires_at": time.time() + expires_in,
        }
    )


def stored() -> dict[str, Any]:
    return json.loads(scite_auth.token_path().read_text())


# -- registration -------------------------------------------------------------------------------


def test_scite_is_a_search_only_provider_in_the_default_set() -> None:
    assert REGISTRY["scite"] is Scite
    assert "scite" in DEFAULT_SEARCH
    assert (Scite.can_search, Scite.can_locate, Scite.cacheable) == (True, False, False)


def test_not_signed_in_is_skipped_without_touching_the_network() -> None:
    http = FakeScite()
    p = Scite(http)
    assert p.available() == (False, "not signed in: run `paper-fetch scite-login`")
    lib, _ = make_library()
    lib.search_providers = [p]
    res = lib.search("anything", web_fallback=False)
    assert res["providers"]["scite"] == {
        "status": "skipped",
        "why": "not signed in: run `paper-fetch scite-login`",
    }
    assert http.calls == 0
    with pytest.raises(ProviderUnavailable, match="scite-login"):
        p.search("anything")


# -- the MCP session and what a hit keeps -------------------------------------------------------


def test_search_opens_a_session_then_calls_the_tool() -> None:
    sign_in()
    http = FakeScite()
    hits = Scite(http).search("open paper", oa_only=False, limit=5)

    assert [m["method"] for m in http.rpc] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
    ]
    assert http.rpc[0]["params"]["clientInfo"]["name"] == "paper-fetch"
    call = http.rpc[2]["params"]
    assert call["name"] == "search_literature"
    assert call["arguments"] == {"term": "open paper", "limit": 5, "user_intent": "paper_search"}
    assert "Mcp-Session-Id" not in http.headers[0]
    assert http.headers[2]["Mcp-Session-Id"] == "sess-1"
    assert http.headers[2]["MCP-Protocol-Version"] == "2025-06-18"
    assert "text/event-stream" in http.headers[2]["Accept"]

    a, b = hits
    assert (a.provider, a.title, a.year, a.ids, a.is_oa) == (
        "scite",
        "An open paper",
        2013,
        {"doi": "10.5555/open.1"},
        True,
    )
    assert a.authors == ["Author 0", "Author 1", "Author 2"]
    assert a.locations == []
    assert a.extra == {
        "journal": "journal of examples",
        "oa_status": "gold",
        "tally": {
            "total": 9,
            "supporting": 3,
            "contrasting": 1,
            "mentioning": 5,
            "citingPublications": 7,
        },
        "editorial_notices": [
            {"status": "retracted", "noticeDoi": "10.5555/notice", "date": "2020"}
        ],
    }
    assert (b.year, b.is_oa, b.extra["tally"]) == (2001, False, {"total": 0})


def test_a_hit_never_keeps_access_links_abstracts_or_citation_statements() -> None:
    """scite's access links carry the account's email address."""
    sign_in()
    kept = json.dumps([h.to_json() for h in Scite(FakeScite()).search("x", oa_only=False)])
    for leak in ("example.org", "delivery.example", "abstract", "sourceDoi", "doi.org"):
        assert leak not in kept


def test_a_second_search_reuses_the_session() -> None:
    sign_in()
    http = FakeScite()
    p = Scite(http)
    p.search("one", oa_only=False)
    p.search("two", oa_only=False)
    assert [m["method"] for m in http.rpc].count("initialize") == 1


def test_oa_only_asks_for_more_and_drops_what_scite_marks_closed() -> None:
    sign_in()
    http = FakeScite()
    hits = Scite(http).search("x", oa_only=True, limit=10)
    assert [h.ids["doi"] for h in hits] == ["10.5555/open.1"]
    assert http.rpc[2]["params"]["arguments"]["limit"] == 30
    http2 = FakeScite()
    Scite(http2).search("x", oa_only=True, limit=40)
    assert http2.rpc[2]["params"]["arguments"]["limit"] == Scite.MAX


def test_an_event_stream_answer_is_read() -> None:
    sign_in()
    http = FakeScite()
    http.sse = True
    assert len(Scite(http).search("x", oa_only=False)) == 2


def test_results_are_not_written_to_the_store() -> None:
    sign_in()
    store = MemoryStore()
    lib, _ = make_library(store=store)
    lib.search_providers = [Scite(FakeScite())]
    res = lib.search("x", oa_only=False, web_fallback=False)
    assert res["providers"]["scite"]["status"] == "ok"
    assert not [k for k in store.keys("papers/") if "searches/scite" in k]


# -- failure is by name ---------------------------------------------------------------------------


def test_an_expiring_token_is_refreshed_before_the_call() -> None:
    sign_in(expires_in=30)
    http = FakeScite()
    assert len(Scite(http).search("x", oa_only=False)) == 2
    assert http.refreshes == 1
    assert http.forms[0] == {
        "grant_type": "refresh_token",
        "refresh_token": "refresh-1",
        "client_id": "client-xyz",
        "resource": "https://api.scite.ai/mcp",
    }
    assert (stored()["access_token"], stored()["refresh_token"]) == ("access-2", "refresh-2")


def test_a_refresh_without_rotation_keeps_the_refresh_token() -> None:
    sign_in(expires_in=30)
    http = FakeScite()
    http.rotate = False
    Scite(http).search("x", oa_only=False)
    assert stored()["refresh_token"] == "refresh-1"


def test_a_401_gets_one_refresh_and_one_retry() -> None:
    sign_in("stale")  # not expired by the clock, but the server no longer accepts it
    http = FakeScite()
    assert len(Scite(http).search("x", oa_only=False)) == 2
    assert http.refreshes == 1


def test_a_refused_refresh_says_to_sign_in_again() -> None:
    sign_in("stale")
    http = FakeScite()
    http.refresh_ok = False
    with pytest.raises(ProviderUnavailable, match="run `paper-fetch scite-login`"):
        Scite(http).search("x")


def test_a_refresh_token_another_process_just_spent_is_not_an_expiry() -> None:
    """scite rotates refresh tokens; two processes share one sign-in file."""
    sign_in("stale", refresh="spent")

    class Raced(FakeScite):
        def post(self, url: str, body: bytes, headers: Any = None, **_k: Any) -> Response:
            if url == META["token_endpoint"]:
                sign_in("access-1", refresh="successor")  # the other process got there first
                return J({"error": "invalid_grant"}, 400)
            return super().post(url, body, headers)

    assert len(Scite(Raced()).search("x", oa_only=False)) == 2
    assert stored()["refresh_token"] == "successor"


def test_a_merged_hit_carries_the_scite_tally_and_notices() -> None:
    sign_in()
    lib, _ = make_library()
    lib.search_providers = [Scite(FakeScite())]
    hits = {h["ids"]["doi"]: h for h in lib.search("x", oa_only=False, web_fallback=False)["hits"]}
    assert hits["10.5555/open.1"]["scite"] == {
        "tally": {
            "total": 9,
            "supporting": 3,
            "contrasting": 1,
            "mentioning": 5,
            "citingPublications": 7,
        },
        "oa_status": "gold",
        "editorial_notices": [
            {"status": "retracted", "noticeDoi": "10.5555/notice", "date": "2020"}
        ],
    }
    assert hits["10.5555/closed.2"]["scite"] == {"tally": {"total": 0}, "oa_status": "closed"}


@pytest.mark.parametrize(("status", "match"), [(429, "rate-limited"), (500, "HTTP 500")])
def test_a_refusal_is_unavailable_not_empty(status: int, match: str) -> None:
    sign_in()
    http = FakeScite()
    http.status_for_call = status
    with pytest.raises(ProviderUnavailable, match=match):
        Scite(http).search("x")


def test_a_tool_error_is_unavailable() -> None:
    sign_in()
    http = FakeScite()
    http.tool_error = True
    with pytest.raises(ProviderUnavailable, match="scite is busy"):
        Scite(http).search("x")


def test_a_forgotten_session_is_reopened_once() -> None:
    sign_in()
    http = FakeScite()
    http.forget_session_once = True
    assert len(Scite(http).search("x", oa_only=False)) == 2
    assert [m["method"] for m in http.rpc].count("initialize") == 2


def test_an_unreachable_server_is_unavailable() -> None:
    sign_in()

    class Dead(FakeScite):
        def post(self, *_a: Any, **_k: Any) -> Response:
            raise ConnectionError("timed out")

    with pytest.raises(ProviderUnavailable, match="scite unreachable"):
        Scite(Dead()).search("x")


# -- signing in -----------------------------------------------------------------------------------


def _browser(answer: dict[str, str] | None = None, *, tamper_state: bool = False) -> Any:
    """Plays the user: follows the authorize URL straight back to the loopback listener."""
    seen: dict[str, Any] = {}

    def open_browser(url: str) -> None:
        q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}
        seen.update(q)
        back = answer if answer is not None else {"code": "the-code"}
        back = {**back, "state": "wrong" if tamper_state else q["state"]}
        target = f"{q['redirect_uri']}?{urllib.parse.urlencode(back)}"

        def go() -> None:
            import http.client  # noqa: PLC0415

            u = urllib.parse.urlsplit(target)
            c = http.client.HTTPConnection(u.hostname or "", u.port, timeout=5)
            c.request("GET", f"{u.path}?{u.query}")
            c.getresponse().read()
            c.close()

        threading.Thread(target=go, daemon=True).start()

    open_browser.seen = seen  # type: ignore[attr-defined]
    return open_browser


def test_login_registers_proves_possession_and_stores_the_sign_in_privately() -> None:
    http = FakeScite()
    browser = _browser()
    said: list[str] = []
    path = scite_auth.login(http, open_browser=browser, say=said.append, timeout_s=10)

    q = browser.seen
    assert http.registered["token_endpoint_auth_method"] == "none"
    assert http.registered["redirect_uris"] == [q["redirect_uri"]]
    assert q["redirect_uri"].startswith("http://127.0.0.1:")
    assert (q["client_id"], q["scope"], q["resource"]) == (
        "client-xyz",
        "mcp offline_access",
        "https://api.scite.ai/mcp",
    )
    form = http.forms[0]
    assert (form["grant_type"], form["code"], form["redirect_uri"]) == (
        "authorization_code",
        "the-code",
        q["redirect_uri"],
    )
    digest = hashlib.sha256(form["code_verifier"].encode()).digest()
    assert q["code_challenge"] == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert q["code_challenge_method"] == "S256"

    assert path == scite_auth.token_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    st = stored()
    assert (st["client_id"], st["access_token"], st["refresh_token"]) == (
        "client-xyz",
        "access-1",
        "refresh-1",
    )
    assert scite_auth.signed_in()
    assert "Sign in to scite" in said[0]
    assert len(Scite(http).search("x", oa_only=False)) == 2  # and the sign-in works


def test_login_refuses_an_answer_for_a_different_request() -> None:
    with pytest.raises(scite_auth.SciteAuthError, match="did not match"):
        scite_auth.login(
            FakeScite(), open_browser=_browser(tamper_state=True), say=lambda _s: None, timeout_s=10
        )
    assert not scite_auth.signed_in()


def test_login_reports_a_refusal_and_stores_nothing() -> None:
    browser = _browser({"error": "access_denied", "error_description": "you said no"})
    with pytest.raises(scite_auth.SciteAuthError, match="you said no"):
        scite_auth.login(FakeScite(), open_browser=browser, say=lambda _s: None, timeout_s=10)
    assert not scite_auth.signed_in()


def test_login_gives_up_when_nobody_signs_in() -> None:
    with pytest.raises(scite_auth.SciteAuthError, match="no sign-in arrived"):
        scite_auth.login(
            FakeScite(), open_browser=lambda _u: None, say=lambda _s: None, timeout_s=0.2
        )


def test_logout_forgets_the_sign_in() -> None:
    sign_in()
    assert scite_auth.logout() is True
    assert not scite_auth.signed_in()
    assert scite_auth.logout() is False


def test_a_corrupt_sign_in_file_is_not_signed_in(tmp_path: Path) -> None:
    scite_auth.token_path().parent.mkdir(parents=True, exist_ok=True)
    scite_auth.token_path().write_text("{not json")
    assert not scite_auth.signed_in()
    with pytest.raises(scite_auth.SciteAuthError, match="not signed in"):
        scite_auth.access_token(FakeScite())


# -- the command line -----------------------------------------------------------------------------


def test_cli_lists_scite_and_can_sign_out(capsys: pytest.CaptureFixture[str]) -> None:
    lib, _ = make_library()
    assert cli.main(["providers"], library=lib) == 0
    out = capsys.readouterr().out
    assert "scite" in out
    assert "scite-login" in out
    assert cli.main(["scite-logout"], library=lib) == 0
    assert "no scite sign-in on file" in capsys.readouterr().out
    sign_in()
    assert cli.main(["scite-logout"], library=lib) == 0
    assert "scite sign-in removed" in capsys.readouterr().out


def test_cli_login_uses_the_library_http(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lib, http = make_library()
    seen: list[Any] = []

    def fake_login(h: Any) -> Path:
        seen.append(h)
        return Path("/somewhere/scite-oauth.json")

    monkeypatch.setattr(scite_auth, "login", fake_login)
    assert cli.main(["scite-login"], library=lib) == 0
    assert seen == [http]
    assert "signed in to scite" in capsys.readouterr().out


# -- Http.post ------------------------------------------------------------------------------------


class _Resp(io.BytesIO):
    status = 200
    headers = Message()

    def geturl(self) -> str:
        return "https://final.example/x"

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def test_post_sends_the_body_and_counts_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_urlopen(req: Any, timeout: float) -> _Resp:
        seen.update(body=req.data, auth=req.get_header("Authorization"), method=req.get_method())
        return _Resp(b"ok")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    h = Http()
    r = h.post("https://x.example/a", b"payload", {"Authorization": "Bearer t"})
    assert (r.status, r.body) == (200, b"ok")
    assert seen == {"body": b"payload", "auth": "Bearer t", "method": "POST"}
    assert (h.calls, h.bytes_in, h.log) == (1, 2, [("https://x.example/a", 200, 2)])


def test_post_returns_a_4xx_and_raises_on_5xx_or_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    def raiser(code: int) -> Any:
        def go(*_a: Any, **_k: Any) -> None:
            raise urllib.error.HTTPError("https://x", code, "no", Message(), io.BytesIO(b"why"))

        return go

    monkeypatch.setattr("urllib.request.urlopen", raiser(401))
    r = Http().post("https://x", b"")
    assert (r.status, r.body) == (401, b"why")
    monkeypatch.setattr("urllib.request.urlopen", raiser(503))
    with pytest.raises(ConnectionError, match="HTTP 503"):
        Http().post("https://x", b"")

    def down(*_a: Any, **_k: Any) -> None:
        raise OSError("no route")

    monkeypatch.setattr("urllib.request.urlopen", down)
    with pytest.raises(ConnectionError, match="no route"):
        Http().post("https://x", b"")
