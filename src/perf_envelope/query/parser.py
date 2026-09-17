"""Substitute `{{param}}` placeholders in query documents."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

from perf_envelope.config.models import ParameterSpec, QueryBundle, QueryConfig
from perf_envelope.exceptions import ConfigError

PLACEHOLDER = re.compile(r"^\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$")


def substitute(value: Any, params: dict[str, Any]) -> Any:
    if isinstance(value, str) and value.startswith("{{") and value.endswith("}}"):
        key = value[2:-2].strip()
        return params.get(key, value)
    if isinstance(value, dict):
        if set(value.keys()) == {"value"}:
            return substitute(value["value"], params)
        return {k: substitute(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(item, params) for item in value]
    return value


def build_filter(query: QueryConfig, params: dict[str, Any]) -> dict[str, Any]:
    return substitute(query.filter, params)


def build_pipeline(query: QueryConfig, params: dict[str, Any]) -> list[dict[str, Any]]:
    return substitute(query.pipeline or [], params)


def sort_list(query: QueryConfig) -> list[tuple[str, int]] | None:
    if query.sort is None:
        return None
    if isinstance(query.sort, dict):
        return list(query.sort.items())
    return list(query.sort)


def apply_range_duration(value: Any) -> Any:
    if isinstance(value, str) and value.endswith("d") and value[:-1].lstrip("-").isdigit():
        days = int(value[:-1])
        from datetime import UTC

        return datetime.now(UTC) - timedelta(days=abs(days))
    return value


DATE_HINTS = ("date", "time", "created", "updated", "start", "end", "since", "until")


def query_from_mongo_json(raw: Any, collection: str | None = None) -> QueryBundle:
    """Turn a pasted Mongo filter or aggregation pipeline into a QueryBundle."""
    document = _coerce_json(raw)
    if isinstance(document, list):
        query = QueryConfig(
            id="primary",
            operation="aggregate",
            collection=collection,
            pipeline=document,
        )
        parameters = _merge_parameters(extract_placeholders(document), None)
        return QueryBundle(query=query, parameters=parameters)
    if not isinstance(document, dict):
        raise ConfigError("Query must be a JSON object (find filter) or array (aggregation pipeline)")
    explicit_params = document.get("parameters")
    if "pipeline" in document:
        pipeline = document["pipeline"]
        if not isinstance(pipeline, list):
            raise ConfigError("query.pipeline must be an array")
        query = QueryConfig(
            id=str(document.get("id") or "primary"),
            operation="aggregate",
            collection=collection or document.get("collection"),
            pipeline=pipeline,
        )
        return QueryBundle(
            query=query,
            parameters=_merge_parameters(extract_placeholders(pipeline), explicit_params),
        )
    has_find_keys = any(key in document for key in ("filter", "projection", "sort", "limit"))
    if has_find_keys:
        query = QueryConfig(
            id=str(document.get("id") or "primary"),
            operation="find",
            collection=collection or document.get("collection"),
            filter=dict(document.get("filter") or {}),
            projection=document.get("projection"),
            sort=document.get("sort"),
            limit=document.get("limit"),
        )
        return QueryBundle(
            query=query,
            parameters=_merge_parameters(extract_placeholders(query.filter), explicit_params),
        )
    query = QueryConfig(
        id="primary",
        operation="find",
        collection=collection,
        filter=document,
    )
    return QueryBundle(
        query=query,
        parameters=_merge_parameters(extract_placeholders(document), explicit_params),
    )


def parameter_spec_for_placeholder(name: str, field: str) -> ParameterSpec:
    """Choose a strategy so selectivity sweeps actually change match volume."""
    lowered = f"{name} {field}".lower()
    if any(token in lowered for token in DATE_HINTS):
        return ParameterSpec(
            type="datetime_range",
            field=field,
            strategy="range",
            range=["7d", "30d", "90d"],
        )
    return ParameterSpec(type="dataset_value", field=field, strategy="selectivity_targeted")


def extract_placeholders(tree: Any) -> dict[str, ParameterSpec]:
    found: dict[str, ParameterSpec] = {}

    def walk(node: Any, field_hint: str | None) -> None:
        if isinstance(node, str):
            match = PLACEHOLDER.match(node)
            if not match:
                return
            name = match.group(1)
            field = field_hint if field_hint and field_hint != "value" else name
            found.setdefault(name, parameter_spec_for_placeholder(name, field))
            return
        if isinstance(node, dict):
            if set(node.keys()) == {"value"}:
                walk(node["value"], field_hint)
                return
            for key, value in node.items():
                next_field = field_hint
                if isinstance(key, str) and not key.startswith("$") and key != "value":
                    next_field = key
                walk(value, next_field)
            return
        if isinstance(node, list):
            for item in node:
                walk(item, field_hint)

    walk(tree, None)
    return found


def _merge_parameters(
    inferred: dict[str, ParameterSpec], explicit: Any
) -> dict[str, ParameterSpec]:
    merged = dict(inferred)
    if not explicit:
        return merged
    if not isinstance(explicit, dict):
        raise ConfigError("query.parameters must be an object")
    for key, value in explicit.items():
        if isinstance(value, ParameterSpec):
            merged[str(key)] = value
        elif isinstance(value, dict):
            merged[str(key)] = ParameterSpec.model_validate(value)
        else:
            raise ConfigError(f"Invalid parameter spec for '{key}'")
    return merged


def _coerce_json(raw: Any) -> Any:
    if raw is None or raw == "":
        raise ConfigError("Query is empty")
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str):
        raise ConfigError("Query must be JSON text, an object, or an array")
    text = raw.strip()
    if not text:
        raise ConfigError("Query is empty")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Query is not valid JSON: {exc}") from exc
