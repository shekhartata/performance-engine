"""Execute a single MongoDB operation and return elapsed milliseconds."""

from __future__ import annotations

import time
from typing import Any

from pymongo.collection import Collection

from perf_envelope.config.models import QueryConfig
from perf_envelope.query.parser import build_filter, build_pipeline, sort_list


class QueryResult:
    __slots__ = ("ok", "latency_ms", "returned", "error", "timeout")

    def __init__(self, ok: bool, latency_ms: float, returned: int = 0, error: str | None = None, timeout: bool = False):
        self.ok = ok
        self.latency_ms = latency_ms
        self.returned = returned
        self.error = error
        self.timeout = timeout


def execute_once(
    collection: Collection,
    query: QueryConfig,
    params: dict[str, Any],
    timeout_ms: int,
) -> QueryResult:
    t0 = time.perf_counter()
    try:
        count = _run(collection, query, params, timeout_ms)
        latency = (time.perf_counter() - t0) * 1000
        return QueryResult(True, latency, returned=count)
    except Exception as exc:  # noqa: BLE001
        latency = (time.perf_counter() - t0) * 1000
        message = str(exc).lower()
        timeout = "timeout" in message or "exceeded" in message and "time" in message
        return QueryResult(False, latency, error=str(exc), timeout=timeout)


def _run(collection: Collection, query: QueryConfig, params: dict[str, Any], timeout_ms: int) -> int:
    if query.operation == "aggregate":
        cursor = collection.aggregate(build_pipeline(query, params), maxTimeMS=timeout_ms)
        return sum(1 for _ in cursor)
    if query.operation == "count":
        return collection.count_documents(build_filter(query, params), maxTimeMS=timeout_ms)
    if query.operation == "distinct":
        values = collection.distinct(query.key or "_id", build_filter(query, params))
        return len(values)
    kwargs: dict[str, Any] = {"max_time_ms": timeout_ms}
    if query.projection:
        kwargs["projection"] = query.projection
    if query.sort:
        kwargs["sort"] = sort_list(query)
    if query.limit:
        kwargs["limit"] = query.limit
    cursor = collection.find(build_filter(query, params), **kwargs)
    return sum(1 for _ in cursor)
