"""Signing in to scite's MCP server, and keeping that sign-in usable.

scite's search API wants a partner token; its MCP server (`https://api.scite.ai/mcp`) instead takes
the subscriber's own sign-in, by OAuth 2.1 with dynamic client registration and PKCE. So there is
no key to configure: `paper-fetch scite-login` registers this machine as a public client, opens
the browser once, and stores what comes back.

- **Where it is kept:** `scite-oauth.json` beside the `*.env` files (`~/.config/paper-fetch/`, or
  `$PAPER_FETCH_ENV_DIR`), mode 0600 in a 0700 directory. Never in the store: a bucket is shared
  between machines and projects, and a sign-in is one person's.
- **Refreshing:** the access token (15 minutes, measured 2026-10-05) is renewed with the refresh
  token when it has under a minute left. scite ROTATES the refresh token on every use, and several
  processes (the CLI, MCP servers) share the file, so it is re-read before each refresh, and a
  refused refresh re-reads it once more: if another process has just spent the token and written
  its successor, that sign-in is used instead of reporting an expiry.
- **Headless use** (the MCP server, a scheduled run) needs no browser once signed in. When the
  refresh token itself is refused, the provider reports `unavailable` and says to sign in again.

All requests go through `Http`; the only socket opened here is the loopback listener that
receives the browser's redirect during `login`.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import secrets
import tempfile
import time
import urllib.parse
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from .config import env_dir
from .http import HttpClient

__all__ = [
    "SciteAuthError",
    "access_token",
    "login",
    "logout",
    "refresh",
    "signed_in",
    "token_path",
]

ISSUER = "https://api.scite.ai"
RESOURCE = "https://api.scite.ai/mcp"
SCOPES = "mcp offline_access"
_FORM = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
_JSON = {"Content-Type": "application/json", "Accept": "application/json"}


class SciteAuthError(RuntimeError):
    """Not signed in, or the sign-in was refused. The message says what to do."""


def token_path() -> Path:
    return env_dir() / "scite-oauth.json"


def _load() -> dict[str, Any] | None:
    try:
        d = json.loads(token_path().read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _save(state: dict[str, Any]) -> None:
    path = token_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".scite-oauth-")
    try:
        with os.fdopen(fd, "w") as fh:  # mkstemp creates the file 0600
            json.dump(state, fh, indent=1)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def signed_in() -> bool:
    """A sign-in is on file. Says nothing about whether scite still accepts it. No network."""
    st = _load()
    return bool(st and st.get("client_id") and (st.get("refresh_token") or st.get("access_token")))


def logout() -> bool:
    """Forget the sign-in on this machine. True if there was one."""
    try:
        token_path().unlink()
    except FileNotFoundError:
        return False
    return True


def _answer(r: Any, what: str, ok: tuple[int, ...] = (200,)) -> dict[str, Any]:
    if r.status not in ok:
        detail = r.body[:200].decode("utf-8", "replace")
        raise SciteAuthError(f"scite {what} failed (HTTP {r.status}): {detail}")
    out = json.loads(r.body)
    if not isinstance(out, dict):
        raise SciteAuthError(f"scite {what} returned no object")
    return out


def _keep(state: dict[str, Any], tok: dict[str, Any]) -> dict[str, Any]:
    if not tok.get("access_token"):
        raise SciteAuthError("scite returned no access token")
    state["access_token"] = tok["access_token"]
    state["expires_at"] = time.time() + float(tok.get("expires_in") or 3600)
    if tok.get("refresh_token"):  # rotated, or issued for the first time
        state["refresh_token"] = tok["refresh_token"]
    return state


def refresh(http: HttpClient) -> str:
    """Renew the access token with the refresh token on file; returns the new access token."""
    st = _load()
    if not st or not st.get("refresh_token"):
        raise SciteAuthError("not signed in to scite: run `paper-fetch scite-login`")
    r = http.post(
        st["token_endpoint"],
        urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": st["refresh_token"],
                "client_id": st["client_id"],
                "resource": RESOURCE,
            }
        ).encode(),
        _FORM,
    )
    if r.status in (400, 401, 403):
        # scite rotates the refresh token, so another process may have just spent this one.
        now = _load()
        if now and now.get("refresh_token") != st["refresh_token"] and now.get("access_token"):
            return str(now["access_token"])
        raise SciteAuthError("scite sign-in has expired: run `paper-fetch scite-login`")
    _save(_keep(st, _answer(r, "token refresh")))
    return str(st["access_token"])


def access_token(http: HttpClient) -> str:
    """A usable access token, refreshed if it is about to expire."""
    st = _load()
    if not st:
        raise SciteAuthError("not signed in to scite: run `paper-fetch scite-login`")
    if st.get("access_token") and float(st.get("expires_at") or 0) - time.time() > 60:
        return str(st["access_token"])
    return refresh(http)


class _Callback(BaseHTTPRequestHandler):
    """Receives the one redirect from the browser and keeps its query string."""

    got: dict[str, str]

    def do_GET(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        if url.path != "/callback":
            self.send_error(404)
            return
        q = urllib.parse.parse_qs(url.query)
        type(self).got = {k: v[0] for k, v in q.items()}
        ok = "code" in q
        body = (
            "<p>paper-fetch is signed in to scite. You can close this tab.</p>"
            if ok
            else "<p>scite did not sign paper-fetch in. See the terminal.</p>"
        ).encode()
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return None


def login(
    http: HttpClient,
    *,
    open_browser: Callable[[str], Any] = webbrowser.open,
    say: Callable[[str], Any] = print,
    timeout_s: float = 300.0,
) -> Path:
    """Sign in through the browser and store the result; returns the file written.

    Registers a new public client each time (scite's registration endpoint is open), asks for
    the `mcp` and `offline_access` scopes, and proves possession with PKCE (S256).
    """
    r = http.get(f"{ISSUER}/.well-known/oauth-authorization-server", {"Accept": "application/json"})
    meta = _answer(r, "sign-in discovery")

    handler = type("_Handler", (_Callback,), {"got": {}})
    server = HTTPServer(("127.0.0.1", 0), handler)
    try:
        redirect = f"http://127.0.0.1:{server.server_address[1]}/callback"
        reg = _answer(
            http.post(
                meta["registration_endpoint"],
                json.dumps(
                    {
                        "client_name": "paper-fetch",
                        "redirect_uris": [redirect],
                        "grant_types": ["authorization_code", "refresh_token"],
                        "response_types": ["code"],
                        "token_endpoint_auth_method": "none",
                        "scope": SCOPES,
                    }
                ).encode(),
                _JSON,
            ),
            "client registration",
            ok=(200, 201),
        )
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        state = secrets.token_urlsafe(24)
        url = (
            meta["authorization_endpoint"]
            + "?"
            + urllib.parse.urlencode(
                {
                    "response_type": "code",
                    "client_id": reg["client_id"],
                    "redirect_uri": redirect,
                    "scope": SCOPES,
                    "state": state,
                    "code_challenge": challenge.rstrip(b"=").decode(),
                    "code_challenge_method": "S256",
                    "resource": RESOURCE,
                }
            )
        )
        say(f"Sign in to scite in the browser. If it did not open, visit:\n{url}")
        open_browser(url)
        server.timeout = 1.0
        deadline = time.monotonic() + timeout_s
        while not handler.got and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    got: dict[str, str] = handler.got
    if not got:
        raise SciteAuthError(f"no sign-in arrived within {int(timeout_s)} s")
    if got.get("state") != state:
        raise SciteAuthError("the sign-in answer did not match this request; nothing was stored")
    if "code" not in got:
        why = got.get("error_description") or got.get("error") or "no code"
        raise SciteAuthError(f"scite refused the sign-in: {why}")
    tok = _answer(
        http.post(
            meta["token_endpoint"],
            urllib.parse.urlencode(
                {
                    "grant_type": "authorization_code",
                    "code": got["code"],
                    "redirect_uri": redirect,
                    "client_id": reg["client_id"],
                    "code_verifier": verifier,
                    "resource": RESOURCE,
                }
            ).encode(),
            _FORM,
        ),
        "token exchange",
    )
    _save(_keep({"client_id": reg["client_id"], "token_endpoint": meta["token_endpoint"]}, tok))
    return token_path()
