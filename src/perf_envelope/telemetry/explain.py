"""Capture explain() execution stats."""

from __future__ import annotations

from typing import Any

from pymongo.collection import Collection

from perf_envelope.config.models import QueryConfig
from perf_envelope.query.parser import build_filter, build_pipeline, sort_list


def explain_query(collection: Collection, query: QueryConfig, params: dict[str, Any]) -> dict[str, Any]:
    try:
        if query.operation == "aggregate":
            cursor = collection.aggregate(build_pipeline(query, params))
            raw = cursor.explain() if hasattr(cursor, "explain") else {}
            if not raw:
                # Fall back to running explain via command.
                raw = collection.database.command(
                    {
                        "explain": {
                            "aggregate": collection.name,
                            "pipeline": build_pipeline(query, params),
                            "cursor": {},
                        },
                        "verbosity": "executionStats",
                    }
                )
        else:
            kwargs: dict[str, Any] = {}
            if query.projection:
                kwargs["projection"] = query.projection
            if query.sort:
                kwargs["sort"] = sort_list(query)
            if query.limit:
                kwargs["limit"] = query.limit
            cursor = collection.find(build_filter(query, params), **kwargs)
            raw = cursor.explain()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    return _extract_stats(raw)


def _extract_stats(raw: dict[str, Any]) -> dict[str, Any]:
    exec_stats = raw.get("executionStats") or {}
    if not exec_stats and "queryPlanner" in raw:
        exec_stats = raw
    winning = (raw.get("queryPlanner") or {}).get("winningPlan") or {}
    n_returned = exec_stats.get("nReturned") or exec_stats.get("nreturned") or 0
    keys = exec_stats.get("totalKeysExamined") or 0
    docs = exec_stats.get("totalDocsExamined") or 0
    millis = exec_stats.get("executionTimeMillis") or exec_stats.get("executionTimeMillisEstimate") or 0
    return {
        "n_returned": int(n_returned),
        "keys_examined": int(keys),
        "docs_examined": int(docs),
        "execution_time_ms": int(millis),
        "keys_examined_per_returned": (keys / n_returned) if n_returned else float(keys),
        "docs_examined_per_returned": (docs / n_returned) if n_returned else float(docs),
        "stage": winning.get("stage") or (winning.get("inputStage") or {}).get("stage"),
        "raw_winning_plan": winning,
    }


def summarize_explains(samples: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [s for s in samples if "error" not in s]
    if not usable:
        return {
            "n_returned": 0,
            "keys_examined": 0,
            "docs_examined": 0,
            "execution_time_ms": 0,
            "keys_examined_per_returned": 0.0,
            "docs_examined_per_returned": 0.0,
            "samples": samples,
        }
    def avg(key: str) -> float:
        return float(sum(s.get(key, 0) for s in usable) / len(usable))

    return {
        "n_returned": avg("n_returned"),
        "keys_examined": avg("keys_examined"),
        "docs_examined": avg("docs_examined"),
        "execution_time_ms": avg("execution_time_ms"),
        "keys_examined_per_returned": avg("keys_examined_per_returned"),
        "docs_examined_per_returned": avg("docs_examined_per_returned"),
        "stage": usable[0].get("stage"),
        "samples": samples,
    }
