"""The only place this package touches the network, so it can be counted and replaced.

Counting matters: the whole promise of the library is "a paper we already have is never downloaded
again", and the only honest test of that promise is a counter on the one function that downloads.
Tests replace this class with a fake that serves recorded responses; nothing else opens a socket
(the one exception is `S3Store`, which talks to its bucket through boto3).
"""

from __future__ import annotations

import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

__all__ = ["Http", "HttpClient", "Response", "user_agent"]


def user_agent() -> str:
    """`paper-fetch/<version>`, plus a mailto when PAPER_FETCH_EMAIL is set (several APIs ask for
    one)."""
    from . import __version__  # noqa: PLC0415 -- avoids an import cycle with the package root

    email = os.environ.get("PAPER_FETCH_EMAIL")
    return f"paper-fetch/{__version__}" + (f" (mailto:{email})" if email else "")


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str


class HttpClient(Protocol):
    """What every caller needs from `Http`; tests supply their own implementation."""

    def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        timeout: float | None = None,
        retries: int | None = None,
    ) -> Response: ...


@dataclass
class Http:
    timeout: float = 45.0
    retries: int = 2
    calls: int = 0
    bytes_in: int = 0
    log: list[tuple[str, int | None, int]] = field(default_factory=list)
    # One Http instance is shared by every request an MCP server dispatches; the lock keeps the
    # counters exact if requests run concurrently.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        timeout: float | None = None,
        retries: int | None = None,
    ) -> Response:
        h = {"User-Agent": user_agent(), **(headers or {})}
        last: Exception | None = None
        tries = (self.retries if retries is None else retries) + 1
        for attempt in range(tries):
            with self._lock:
                self.calls += 1
            try:
                req = urllib.request.Request(url, headers=h)
                with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                    body = r.read()
                    with self._lock:
                        self.bytes_in += len(body)
                        self.log.append((url, r.status, len(body)))
                    return Response(
                        r.status, {k.lower(): v for k, v in r.headers.items()}, body, r.geturl()
                    )
            except urllib.error.HTTPError as e:
                body = e.read() if e.fp is not None else b""
                with self._lock:
                    self.log.append((url, e.code, len(body)))
                # 4xx is an answer, not a transient failure -- INCLUDING 429. Retrying a 429 and
                # then raising a generic error would hide the rate limit from every caller, and a
                # federated search would report a throttled provider as a generic "error" after
                # minutes of back-off. The caller decides what a 429 means.
                if e.code < 500:
                    hdrs = {k.lower(): v for k, v in (e.headers or {}).items()}
                    return Response(e.code, hdrs, body, url)
                last = e
            except Exception as e:  # noqa: BLE001 -- any transport failure is retried, then raised
                last = e
                with self._lock:
                    self.log.append((url, None, 0))
            if attempt + 1 < tries:
                time.sleep(0.8 * (attempt + 1))
        raise ConnectionError(f"GET {url} failed after {tries} attempts: {last}")
