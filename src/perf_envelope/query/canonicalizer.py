"""Canonical query-shape identity. Literals never affect the shape hash."""

from __future__ import annotations

import hashlib
from typing import Any

from perf_envelope.config.models import QueryConfig


def _canon_value(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value.keys()) == {"value"}:
            return "?"
        if len(value) == 1:
            key, inner = next(iter(value.items()))
            if key.startswith("$") and key not in {"$and", "$or", "$nor"}:
                return {key: "?"}
        return {k: _canon_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_canon_value(v) for v in value]
    if isinstance(value, str) and value.startswith("{{"):
        return "?"
    return "?"


def canonical_form(query: QueryConfig) -> str:
    lines = [f"{query.operation} {query.collection}", ""]
    if query.operation == "aggregate":
        lines.append("pipeline:")
        for stage in query.pipeline or []:
            name = next(iter(stage)) if stage else "?"
            lines.append(name)
        text = "\n".join(lines).strip() + "\n"
        return text
    lines.append("filter:")
    for field, predicate in (query.filter or {}).items():
        rendered = _render_predicate(field, predicate)
        lines.extend(rendered)
    if query.sort:
        lines.append("")
        lines.append("sort:")
        items = query.sort.items() if isinstance(query.sort, dict) else query.sort
        for field, direction in items:
            label = "DESC" if int(direction) < 0 else "ASC"
            lines.append(f"{field} {label}")
    if query.limit is not None:
        lines.append("")
        lines.append("limit:")
        lines.append(str(query.limit))
    if query.projection:
        lines.append("")
        lines.append("projection:")
        for field in query.projection:
            lines.append(field)
    return "\n".join(lines).strip() + "\n"


def _render_predicate(field: str, predicate: Any) -> list[str]:
    if isinstance(predicate, dict):
        if "value" in predicate and len(predicate) == 1:
            return [f"{field} = ?"]
        ops = []
        for op, _value in predicate.items():
            if op.startswith("$"):
                symbol = {
                    "$eq": "=",
                    "$gte": ">=",
                    "$gt": ">",
                    "$lte": "<=",
                    "$lt": "<",
                    "$in": "IN",
                    "$ne": "!=",
                }.get(op, op)
                ops.append(f"{field} {symbol} ?")
            elif op == "value":
                ops.append(f"{field} = ?")
            else:
                ops.append(f"{field}.{op} = ?")
        return ops or [f"{field} = ?"]
    return [f"{field} = ?"]


def shape_id(query: QueryConfig) -> str:
    digest = hashlib.sha256(canonical_form(query).encode("utf-8")).hexdigest()
    return digest[:12]
