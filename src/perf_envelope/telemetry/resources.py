"""Best-effort environment resource snapshots from serverStatus."""

from __future__ import annotations

from typing import Any

from pymongo.database import Database


def capture_resources(database: Database) -> dict[str, Any]:
    try:
        status = dict(database.command({"serverStatus": 1}))
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    wt = status.get("wiredTiger") or {}
    cache = wt.get("cache") or {}
    mem = status.get("mem") or {}
    extra = status.get("extra_info") or {}
    network = status.get("network") or {}
    connections = status.get("connections") or {}
    opcounters = status.get("opcounters") or {}
    return {
        "cpu_user_ms": extra.get("user_time_us"),
        "cpu_system_ms": extra.get("system_time_us"),
        "wiredtiger_cache_bytes": cache.get("bytes currently in the cache"),
        "wiredtiger_cache_max_bytes": cache.get("maximum bytes configured"),
        "wiredtiger_cache_dirty_bytes": cache.get("tracked dirty bytes in the cache"),
        "resident_mb": mem.get("resident"),
        "virtual_mb": mem.get("virtual"),
        "connections_current": connections.get("current"),
        "network_bytes_in": network.get("bytesIn"),
        "network_bytes_out": network.get("bytesOut"),
        "opcounters": {k: opcounters.get(k) for k in ("query", "insert", "update", "delete", "getmore", "command")},
    }
