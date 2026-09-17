"""In-memory UI session and run records. URI never leaves the process."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

COOKIE = "pee_session"
LOCK = threading.Lock()


@dataclass
class SessionRecord:
    id: str
    uri: str
    uri_redacted: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class RunRecord:
    id: str
    session_id: str
    database: str
    collection: str
    status: str = "queued"
    progress: dict[str, Any] | None = None
    error: str | None = None
    analysis: dict[str, Any] | None = None
    observations: list[dict[str, Any]] | None = None
    run_dir: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


SESSIONS: dict[str, SessionRecord] = {}
RUNS: dict[str, RunRecord] = {}


def reset() -> None:
    with LOCK:
        SESSIONS.clear()
        RUNS.clear()
