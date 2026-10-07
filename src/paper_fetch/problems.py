"""A record of provider defects, kept so they get fixed instead of scrolled past.

A provider can fail in two ways. The service can be down, throttling us or out of allowance
(`ProviderUnavailable`): that passes, and the other providers have answered meanwhile. Or the
provider can be BROKEN (`ProviderBroken`, or any exception the adapter did not expect): the
service changed its answer, rejected our request, or the adapter has a bug. Waiting fixes none of
those, and a search that merely lists `error` among ten statuses is easy to read past.

So every such failure is appended here, one JSON object per line, with the traceback:

    {"at", "provider", "operation", "error", "traceback", "context"}

The file is local to the machine (`PAPER_FETCH_PROBLEMS`, else `provider-problems.jsonl` beside
the passage index), mode 0600, and is never written to the store. `paper-fetch problems` prints
it; `status()` counts it. Recording never raises: a full disk must not turn one broken provider
into a failed search.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = ["log_path", "recent", "record"]

_lock = threading.Lock()
_KEEP = 4000  # characters of traceback: the frames nearest the failure


def log_path() -> Path:
    env = os.environ.get("PAPER_FETCH_PROBLEMS")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("PAPER_FETCH_DATA_DIR") or (
        Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "paper-fetch"
    )
    return Path(base).expanduser() / "provider-problems.jsonl"


def record(
    provider: str, operation: str, exc: BaseException, context: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Append one defect and return the row. Never raises."""
    row = {
        "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provider": provider,
        "operation": operation,
        "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        "traceback": "".join(traceback.format_exception(exc))[-_KEEP:],
        "context": context or {},
    }
    with _lock, contextlib.suppress(OSError):
        path = log_path()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    return row


def recent(n: int = 20) -> list[dict[str, Any]]:
    """The last `n` recorded defects, oldest first. An unreadable log is an empty one."""
    try:
        lines = log_path().read_text().splitlines()
    except OSError:
        return []
    out = []
    for ln in lines[-n:] if n else lines:
        with contextlib.suppress(ValueError):
            out.append(json.loads(ln))
    return out
