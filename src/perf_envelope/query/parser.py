"""Substitute `{{param}}` placeholders in query documents."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

from perf_envelope.config.models import ParameterSpec, QueryBundle, QueryConfig
from perf_envelope.exceptions import ConfigError

PLACEHOLDER = re.compile(r"^\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$")
# Cell-time placeholders may be a bare name or a list slice/index: collections[1:union_count].
CELL_PLACEHOLDER = re.compile(r"^\{\{\s*([^{}]+)\s*\}\}$")
_SLICE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\[([^:\]]*):([^:\]]*)\]$")
_INDEX = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\[([^:\]]+)\]$")
_MISSING = object()


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


def render_for_cell(value: Any, variables: dict[str, Any]) -> Any:
    """Fill per-test-case structure: typed values, list slices, and `$for` expansion.

    Placeholders whose names are not in `variables` are left untouched so a later
    `substitute` can fill per-request parameters.
    """
    return _render(value, variables)


def _render(value: Any, variables: dict[str, Any]) -> Any:
    if isinstance(value, list):
        rendered: list[Any] = []
        for item in value:
            expanded = _expand_for(item, variables)
            if expanded is None:
                rendered.append(_render(item, variables))
            else:
                rendered.extend(expanded)
        return rendered
    if isinstance(value, dict):
        if "$for" in value:
            raise ConfigError("`$for` must be an element of a list so it can expand into stages")
        return {key: _render(item, variables) for key, item in value.items()}
    if isinstance(value, str):
        return _render_string(value, variables)
    return value


def _render_string(value: str, variables: dict[str, Any]) -> Any:
    match = CELL_PLACEHOLDER.match(value)
    if not match:
        return value
    resolved = _resolve_expr(match.group(1).strip(), variables)
    if resolved is _MISSING:
        return value
    return resolved


def _expand_for(item: Any, variables: dict[str, Any]) -> list[Any] | None:
    if not isinstance(item, dict) or "$for" not in item:
        return None
    spec = item["$for"]
    if not isinstance(spec, dict) or "each" not in spec:
        raise ConfigError("`$for` requires an object with `each`")
    emit = item.get("emit")
    if emit is None:
        raise ConfigError("`$for` requires `emit`")
    sequence = _resolve_each(spec["each"], variables)
    alias = str(spec.get("as") or "item")
    parallel = bool(spec.get("parallel"))
    rendered: list[Any] = []
    for element in sequence:
        scope = dict(variables)
        scope[alias] = element
        piece = _render(emit, scope)
        if parallel:
            piece = _stamp_parallel(piece)
        if isinstance(piece, list):
            rendered.extend(piece)
        else:
            rendered.append(piece)
    return rendered


def _stamp_parallel(piece: Any) -> Any:
    if isinstance(piece, dict):
        stamped = dict(piece)
        stamped["parallel"] = True
        return stamped
    if isinstance(piece, list):
        return [_stamp_parallel(item) for item in piece]
    return piece


def _resolve_each(each: Any, variables: dict[str, Any]) -> list[Any]:
    if isinstance(each, list):
        return list(each)
    if not isinstance(each, str):
        raise ConfigError("`$for.each` must be a list or a slice expression")
    resolved = _resolve_expr(each.strip(), variables)
    if resolved is _MISSING or not isinstance(resolved, list):
        raise ConfigError(f"Cannot resolve `$for.each` expression '{each}'")
    return resolved


def _resolve_expr(expr: str, variables: dict[str, Any]) -> Any:
    sliced = _SLICE.match(expr)
    if sliced:
        name, start_text, end_text = sliced.groups()
        sequence = variables.get(name, _MISSING)
        if not isinstance(sequence, list):
            return _MISSING
        start = _bound(start_text, variables, default=0)
        end = _bound(end_text, variables, default=len(sequence))
        return list(sequence[start:end])
    indexed = _INDEX.match(expr)
    if indexed:
        name, index_text = indexed.groups()
        sequence = variables.get(name, _MISSING)
        if not isinstance(sequence, list):
            return _MISSING
        return sequence[_bound(index_text, variables, default=0)]
    if expr in variables:
        return variables[expr]
    return _MISSING


def _bound(text: str, variables: dict[str, Any], default: int) -> int:
    token = text.strip()
    if not token:
        return default
    if token.lstrip("-").isdigit():
        return int(token)
    if token not in variables:
        raise ConfigError(f"Unknown slice bound '{token}'")
    return int(variables[token])


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
