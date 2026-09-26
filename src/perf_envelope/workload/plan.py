"""A timed request is one or more database calls, then an optional client-side combine."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from pydantic import Field

from perf_envelope.config.models import FrozenModel, QueryConfig
from perf_envelope.exceptions import ConfigError
from perf_envelope.query.parser import render_for_cell
from perf_envelope.workload.executor import QueryResult, execute_once


class PlanStep(FrozenModel):
    collection: str
    operation: str = "aggregate"
    filter: dict[str, Any] = Field(default_factory=dict)
    pipeline: list[dict[str, Any]] | None = None
    projection: dict[str, Any] | None = None
    sort: dict[str, int] | None = None
    limit: int | None = None
    parallel: bool = False


class CombineSpec(FrozenModel):
    mode: str = "concat"
    field: str = "created_at"
    direction: int = -1


@dataclass
class RenderedPlan:
    steps: list[PlanStep]
    combine: CombineSpec = field(default_factory=CombineSpec)

    def as_dict(self) -> dict[str, Any]:
        return {
            "steps": [step.model_dump() for step in self.steps],
            "combine": self.combine.model_dump(),
        }


def parallel_width(plan: RenderedPlan | None) -> int:
    """Widest run of parallel steps. A single server query has width 1."""
    if plan is None or not plan.steps:
        return 1
    best = 1
    run = 0
    for step in plan.steps:
        if step.parallel:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def render_plan(raw: dict[str, Any], variables: dict[str, Any]) -> RenderedPlan:
    if "steps" not in raw:
        raise ConfigError("A plan requires steps")
    rendered_steps = render_for_cell(raw["steps"], variables)
    if not isinstance(rendered_steps, list) or not rendered_steps:
        raise ConfigError("Plan steps rendered to an empty list")
    steps: list[PlanStep] = []
    for item in rendered_steps:
        steps.append(PlanStep.model_validate(_normalize_step(item)))
    return RenderedPlan(steps=steps, combine=_combine(raw.get("combine")))


def _normalize_step(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ConfigError(f"Plan step must be an object, got {type(item).__name__}")
    if "find" in item and isinstance(item["find"], dict):
        find = item["find"]
        return {
            "collection": find.get("collection"),
            "operation": "find",
            "filter": find.get("filter") or {},
            "projection": find.get("projection"),
            "sort": find.get("sort"),
            "limit": find.get("limit"),
            "parallel": bool(item.get("parallel")),
        }
    return item


def _combine(raw: Any) -> CombineSpec:
    if raw in (None, "", "concat"):
        return CombineSpec(mode="concat")
    if raw == "merge_sort":
        return CombineSpec(mode="merge_sort")
    if isinstance(raw, dict):
        payload = dict(raw)
        if "merge_sort" in payload and "mode" not in payload:
            nested = payload.pop("merge_sort")
            payload["mode"] = "merge_sort"
            if isinstance(nested, dict):
                payload.setdefault("field", next(iter(nested)))
                payload.setdefault("direction", next(iter(nested.values())))
        payload.setdefault("mode", "concat")
        return CombineSpec.model_validate(payload)
    raise ConfigError(f"Unsupported plan combine: {raw}")


def step_query(step: PlanStep) -> QueryConfig:
    operation = step.operation
    if operation not in {"find", "aggregate", "count", "distinct"}:
        raise ConfigError(f"Unsupported plan step operation '{operation}'")
    return QueryConfig(
        id="plan-step",
        operation=operation,  # type: ignore[arg-type]
        collection=step.collection,
        filter=step.filter,
        pipeline=step.pipeline,
        projection=step.projection,
        sort=step.sort,
        limit=step.limit,
    )


def execute_plan(database: Any, plan: RenderedPlan, params: dict[str, Any], timeout_ms: int) -> QueryResult:
    """Run the plan end to end. A single non-parallel step uses the existing count path."""
    if len(plan.steps) == 1 and not plan.steps[0].parallel:
        step = plan.steps[0]
        result = execute_once(database[step.collection], step_query(step), params, timeout_ms)
        result.step_ms = [result.latency_ms]
        return result

    started = time.perf_counter()
    groups = _groups(plan.steps)
    documents: list[dict[str, Any]] = []
    step_ms: list[float] = []
    try:
        for group in groups:
            if len(group) == 1 and not group[0].parallel:
                docs, elapsed = _run_step(database, group[0], params, timeout_ms)
                documents.extend(docs)
                step_ms.append(elapsed)
                continue
            width = max(1, len(group))
            with ThreadPoolExecutor(max_workers=width) as pool:
                futures = [
                    pool.submit(_run_step, database, step, params, timeout_ms) for step in group
                ]
                for future in as_completed(futures):
                    docs, elapsed = future.result()
                    documents.extend(docs)
                    step_ms.append(elapsed)
    except Exception as exc:  # noqa: BLE001
        latency = (time.perf_counter() - started) * 1000
        message = str(exc).lower()
        timeout = "timeout" in message or ("exceeded" in message and "time" in message)
        result = QueryResult(False, latency, error=str(exc), timeout=timeout)
        result.step_ms = step_ms
        return result

    combined = _combine_docs(documents, plan.combine)
    result = QueryResult(True, (time.perf_counter() - started) * 1000, returned=len(combined))
    result.step_ms = step_ms
    return result


def _groups(steps: list[PlanStep]) -> list[list[PlanStep]]:
    groups: list[list[PlanStep]] = []
    current: list[PlanStep] = []
    for step in steps:
        if step.parallel:
            current.append(step)
            continue
        if current:
            groups.append(current)
            current = []
        groups.append([step])
    if current:
        groups.append(current)
    return groups


def _run_step(
    database: Any, step: PlanStep, params: dict[str, Any], timeout_ms: int
) -> tuple[list[dict[str, Any]], float]:
    from perf_envelope.query.parser import build_filter, build_pipeline, sort_list

    query = step_query(step)
    collection = database[step.collection]
    started = time.perf_counter()
    if query.operation == "aggregate":
        cursor = collection.aggregate(build_pipeline(query, params), maxTimeMS=timeout_ms)
        docs = list(cursor)
    elif query.operation == "count":
        count = collection.count_documents(build_filter(query, params), maxTimeMS=timeout_ms)
        docs = [{} for _ in range(int(count))]
    else:
        kwargs: dict[str, Any] = {"max_time_ms": timeout_ms}
        if query.projection:
            kwargs["projection"] = query.projection
        if query.sort:
            kwargs["sort"] = sort_list(query)
        if query.limit:
            kwargs["limit"] = query.limit
        cursor = collection.find(build_filter(query, params), **kwargs)
        docs = list(cursor)
    return docs, (time.perf_counter() - started) * 1000


def _combine_docs(documents: list[dict[str, Any]], combine: CombineSpec) -> list[dict[str, Any]]:
    if combine.mode != "merge_sort":
        return documents
    return sorted(documents, key=lambda doc: _sort_key(doc, combine.field), reverse=combine.direction < 0)


def _sort_key(doc: dict[str, Any], field: str) -> tuple:
    value: Any = doc
    for part in field.split("."):
        if not isinstance(value, dict):
            value = None
            break
        value = value.get(part)
    if value is None:
        return (1, 0)
    return (0, value)
