"""Estimate realized selectivity of a parameterized query."""

from __future__ import annotations

from typing import Any

from pymongo.collection import Collection

from perf_envelope.config.models import QueryConfig
from perf_envelope.query.parser import build_filter


def estimate_selectivity(
    collection: Collection,
    query: QueryConfig,
    params: dict[str, Any],
    *,
    total: int | None = None,
) -> float:
    total = total if total is not None else max(collection.estimated_document_count(), 1)
    if query.operation != "find":
        return min(1.0, (query.limit or 1) / total)
    try:
        matching = collection.count_documents(build_filter(query, params), limit=min(total, 50_000))
    except Exception:  # noqa: BLE001
        matching = query.limit or 1
    return min(1.0, matching / max(total, 1))


def target_match_count(total: int, selectivity: float) -> int:
    return max(1, int(round(total * selectivity)))
