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


def walk_explain(raw: dict[str, Any]) -> dict[str, Any]:
    """Collect per-branch cursor stats and sort spills from an explain document.

    Aggregation explains often have no top-level executionStats. The numbers live on
    each `$cursor`, including cursors nested under `$unionWith`.
    """
    found: dict[str, list] = {"branches": [], "sorts": []}
    root = raw.get("stages") if isinstance(raw.get("stages"), list) else raw
    _walk(root, found, capture_stats=True)
    branches = found["branches"]
    n_returned = sum(int(branch.get("n_returned") or 0) for branch in branches)
    keys = sum(int(branch.get("keys_examined") or 0) for branch in branches)
    docs = sum(int(branch.get("docs_examined") or 0) for branch in branches)
    millis = sum(int(branch.get("execution_time_ms") or 0) for branch in branches)
    return {
        "branches": branches,
        "sort": found["sorts"][-1] if found["sorts"] else None,
        "sorts": found["sorts"],
        "n_returned": n_returned,
        "keys_examined": keys,
        "docs_examined": docs,
        "execution_time_ms": millis,
    }


def _walk(node: Any, found: dict[str, list], *, capture_stats: bool) -> None:
    if isinstance(node, list):
        for item in node:
            _walk(item, found, capture_stats=capture_stats)
        return
    if not isinstance(node, dict):
        return
    if node.get("stage") == "SORT":
        found["sorts"].append(
            {
                "n_returned": node.get("nReturned"),
                "used_disk": bool(node.get("usedDisk") or node.get("spilled")),
                "mem_limit_bytes": node.get("memLimit"),
                "total_data_size_sorted_bytes": node.get("totalDataSizeSorted"),
            }
        )
    stats = node.get("executionStats") if capture_stats else None
    if isinstance(stats, dict):
        found["branches"].append(
            {
                "n_returned": int(stats.get("nReturned") or stats.get("nreturned") or 0),
                "keys_examined": int(stats.get("totalKeysExamined") or 0),
                "docs_examined": int(stats.get("totalDocsExamined") or 0),
                "execution_time_ms": int(
                    stats.get("executionTimeMillis") or stats.get("executionTimeMillisEstimate") or 0
                ),
            }
        )
        for key, value in node.items():
            if key == "executionStats":
                _walk(value, found, capture_stats=False)
            else:
                _walk(value, found, capture_stats=True)
        return
    for value in node.values():
        _walk(value, found, capture_stats=capture_stats)


def _extract_stats(raw: dict[str, Any]) -> dict[str, Any]:
    exec_stats = raw.get("executionStats") or {}
    if not exec_stats and "queryPlanner" in raw:
        exec_stats = raw
    winning = (raw.get("queryPlanner") or {}).get("winningPlan") or {}
    walked = walk_explain(raw)
    n_returned = int(exec_stats.get("nReturned") or exec_stats.get("nreturned") or 0)
    keys = int(exec_stats.get("totalKeysExamined") or 0)
    docs = int(exec_stats.get("totalDocsExamined") or 0)
    millis = int(exec_stats.get("executionTimeMillis") or exec_stats.get("executionTimeMillisEstimate") or 0)
    # Aggregation explains keep the useful numbers on nested cursors, not the root.
    if isinstance(raw.get("stages"), list) and walked["branches"]:
        n_returned = int(walked["n_returned"])
        keys = int(walked["keys_examined"])
        docs = int(walked["docs_examined"])
        millis = int(walked["execution_time_ms"] or millis)
    elif not n_returned and not keys and not docs:
        n_returned = int(walked["n_returned"])
        keys = int(walked["keys_examined"])
        docs = int(walked["docs_examined"])
        millis = int(walked["execution_time_ms"] or millis)
    return {
        "n_returned": int(n_returned),
        "keys_examined": int(keys),
        "docs_examined": int(docs),
        "execution_time_ms": int(millis),
        "keys_examined_per_returned": (keys / n_returned) if n_returned else float(keys),
        "docs_examined_per_returned": (docs / n_returned) if n_returned else float(docs),
        "stage": winning.get("stage") or (winning.get("inputStage") or {}).get("stage"),
        "branches": walked["branches"],
        "sort": walked["sort"],
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
            "branches": [],
            "sort": None,
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
        "branches": usable[0].get("branches") or [],
        "sort": next((sample.get("sort") for sample in usable if sample.get("sort")), None),
        "samples": samples,
    }
