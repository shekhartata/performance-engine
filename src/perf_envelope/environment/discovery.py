"""Discover MongoDB deployment metadata for result provenance."""

from __future__ import annotations

from typing import Any

from pymongo.database import Database

from perf_envelope.environment.mongodb import MongoSession, redact_uri


def _safe_command(database: Database, command: dict[str, Any]) -> dict[str, Any]:
    try:
        return dict(database.command(command))
    except Exception:  # noqa: BLE001 — Atlas may hide privileged commands
        return {}


def discover(session: MongoSession) -> dict[str, Any]:
    db = session.database
    build = _safe_command(db, {"buildInfo": 1})
    hello = _safe_command(db, {"hello": 1}) or _safe_command(db, {"isMaster": 1})
    status = _safe_command(db, {"serverStatus": 1})
    host_info = _safe_command(db, {"hostInfo": 1})

    set_name = hello.get("setName")
    msg = hello.get("msg")
    hosts = hello.get("hosts") or []
    if hello.get("msg") == "isdbgrid" or "mongos" in str(status.get("process", "")):
        topology = "sharded_cluster"
    elif set_name:
        topology = "replica_set"
    elif msg == "standalone" or not hosts:
        topology = "standalone"
    else:
        topology = "unknown"

    wt = status.get("wiredTiger") or {}
    cache = wt.get("cache") or {}
    storage_engine = (status.get("storageEngine") or {}).get("name") or (
        "wiredTiger" if wt else "unknown"
    )

    return {
        "mongodb_version": build.get("version") or status.get("version") or "unknown",
        "git_version": build.get("gitVersion"),
        "topology": topology,
        "replica_set_name": set_name,
        "node_count": len(hosts) if hosts else 1,
        "primary": hello.get("primary") or hello.get("me"),
        "storage_engine": storage_engine,
        "wired_tiger_cache_max_bytes": cache.get("maximum bytes configured"),
        "connections_current": (status.get("connections") or {}).get("current"),
        "host": hello.get("me") or status.get("host"),
        "process": status.get("process"),
        "os": ((host_info.get("os") or {}).get("name")),
        "connection_uri_redacted": session.uri_redacted or redact_uri(""),
        "database": db.name,
    }
