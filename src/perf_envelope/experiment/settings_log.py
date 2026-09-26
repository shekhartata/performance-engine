"""Every value the engine set, with where it came from."""

from __future__ import annotations

import os
import platform
from typing import Any

import pymongo

from perf_envelope.config.loader import ResolvedExperiment
from perf_envelope.config.models import WorkloadConfig
from perf_envelope.environment.pool import pool_formula, required_pool_size


def host_info() -> dict[str, Any]:
    return {
        "hostname": platform.node(),
        "cpu_count": os.cpu_count(),
        "python": platform.python_version(),
        "pymongo": pymongo.version,
        "system": platform.platform(),
    }


def run_level_settings(
    resolved: ResolvedExperiment,
    *,
    rtt_ms: float | None,
    host: dict[str, Any],
) -> list[dict[str, Any]]:
    """Settings that apply to the whole run."""
    user = resolved.extra.get("user_spec") if isinstance(resolved.extra.get("user_spec"), dict) else {}
    rows: list[dict[str, Any]] = []
    rows.append(_row("engine.host", host, "derived", "host running the engine"))
    rows.append(_row("engine.rtt_ms", rtt_ms, "derived", "median of 5 ping commands to the primary"))
    workload_user = user.get("workload") if isinstance(user.get("workload"), dict) else {}
    defaults = WorkloadConfig()
    workload = resolved.workload
    for name in (
        "mode",
        "duration_seconds",
        "request_count",
        "think_time_ms",
        "timeout_ms",
        "warmup_queries",
        "explain_samples",
    ):
        value = getattr(workload, name)
        source = "user" if name in workload_user else ("default" if value == getattr(defaults, name) else "user")
        rows.append(_row(f"workload.{name}", value, source))
    pool = workload.connection_pool
    pool_user = workload_user.get("connection_pool") if isinstance(workload_user.get("connection_pool"), dict) else {}
    rows.append(_row("connection_pool.mode", pool.mode, "user" if "mode" in pool_user else "default"))
    rows.append(
        _row(
            "connection_pool.headroom",
            pool.headroom,
            "user" if "headroom" in pool_user else "default",
        )
    )
    rows.append(
        _row(
            "connection_pool.discovery_max_size",
            pool.max_size,
            "default",
            "client used for discovery and recipe writes, not for measured requests",
        )
    )
    if pool.mode == "fixed":
        rows.append(
            _row(
                "connection_pool.max_size",
                pool.max_size,
                "user" if "max_size" in pool_user or "connection_pool" in workload_user else "default",
                "fixed for every test case",
            )
        )
    else:
        rows.append(
            _row(
                "connection_pool.max_size",
                "per test case",
                "derived",
                "concurrency × parallel steps + headroom",
            )
        )
    execution_user = user.get("execution") if isinstance(user.get("execution"), dict) else {}
    for name in ("repetitions", "max_refinement_rounds", "explain_samples", "warmup_queries"):
        if not hasattr(resolved.execution, name):
            continue
        value = getattr(resolved.execution, name)
        rows.append(_row(f"execution.{name}", value, "user" if name in execution_user else "default"))
    rows.append(_row("slo", resolved.slo.model_dump(), "user" if "slo" in user else "default"))
    rows.append(_row("dataset.mode", resolved.dataset.mode, "user" if "data" in user else "configured"))
    if resolved.dataset.scales:
        rows.append(_row("dataset.scales", {str(k): v for k, v in resolved.dataset.scales.items()}, "user"))
    if resolved.dataset.recipe is not None:
        rows.append(
            _row(
                "dataset.recipe",
                resolved.dataset.recipe.model_dump(by_alias=True),
                "user",
            )
        )
    rows.append(_row("client.appname", "perf-envelope", "default"))
    rows.append(_row("client.server_selection_timeout_ms", 15_000, "default"))
    if resolved.plans:
        rows.append(_row("plans", list(resolved.plans), "user"))
    if resolved.experiment.goal is not None:
        rows.append(_row("goal", resolved.experiment.goal.model_dump(), "user"))
    rows.append(_row("sweep", resolved.experiment.dimensions.as_dict(), "user"))
    rows.append(_row("sweep.bindings", resolved.experiment.dimensions.bindings(), "default"))
    parameters = {
        name: spec.model_dump() for name, spec in resolved.parameters.items()
    }
    rows.append(_row("parameters", parameters, "user" if parameters else "default"))
    rows.append(
        _row(
            "warmup_queries_used",
            resolved.execution.warmup_queries,
            "derived",
            "measurement uses execution.warmup_queries, not workload.warmup_queries",
        )
    )
    rows.append(_row("query", resolved.query.model_dump(), "user"))
    recipe_state = resolved.extra.get("recipe_state") if isinstance(resolved.extra.get("recipe_state"), dict) else None
    if recipe_state:
        rows.append(
            _row(
                "dataset.recipe.fingerprint",
                recipe_state.get("fingerprint"),
                "derived",
                "sha256 of recipe, scale, and matches-per-key",
            )
        )
        rows.append(_row("dataset.recipe.reused", recipe_state.get("reused"), "derived"))
        rows.append(_row("dataset.recipe.created", recipe_state.get("created"), "derived"))
        if recipe_state.get("notes"):
            rows.append(_row("dataset.recipe.notes", recipe_state.get("notes"), "derived"))
    return rows


def cell_settings(
    *,
    model: str,
    cell_values: dict[str, Any],
    bindings: dict[str, str],
    database: str,
    collections: list[str],
    rendered: dict[str, Any] | None,
    concurrency: int,
    parallel_steps: int,
    headroom: int,
    pool_mode: str,
    fixed_pool_size: int,
    pool_stats: dict[str, Any],
    warmup: bool,
    sampled_parameters: dict[str, Any],
    client_cpu_pct: float,
    rtt_ms: float | None,
) -> dict[str, Any]:
    if pool_mode == "fixed":
        pool_size = fixed_pool_size
        formula = f"maxPoolSize={pool_size} (fixed)"
        pool_source = "user"
    else:
        pool_size = required_pool_size(concurrency, parallel_steps, headroom)
        formula = pool_formula(concurrency, parallel_steps, headroom)
        pool_source = "derived"
    settings = []
    for name, value in cell_values.items():
        settings.append(_row(name, value, "user", bind=bindings.get(name)))
    settings.append(_row("database", database, "derived" if database else "default"))
    settings.append(_row("collections", collections, "derived"))
    settings.append(_row("connection_pool.max_size", pool_size, pool_source, formula))
    settings.append(_row("connection_pool.peak_in_use", pool_stats.get("peak_in_use"), "derived"))
    settings.append(_row("connection_pool.wait_p50_ms", pool_stats.get("wait_p50_ms"), "derived"))
    settings.append(_row("connection_pool.wait_p95_ms", pool_stats.get("wait_p95_ms"), "derived"))
    settings.append(_row("connection_pool.wait_max_ms", pool_stats.get("wait_max_ms"), "derived"))
    settings.append(
        _row("connection_pool.connections_created_during_measurement", pool_stats.get("connections_created"), "derived")
    )
    settings.append(_row("parallel_steps", parallel_steps, "derived"))
    settings.append(_row("warmup", warmup, "user" if warmup else "default"))
    settings.append(_row("sampled_parameters", sampled_parameters, "derived"))
    settings.append(_row("client_cpu_pct", round(client_cpu_pct, 2), "derived", "process_time / wall time"))
    settings.append(_row("rtt_ms", rtt_ms, "derived"))
    return {
        "model": model,
        "settings": settings,
        "database": database,
        "collections": collections,
        "rendered": rendered,
        "pool": {
            "max_size": pool_size,
            "formula": formula,
            "source": pool_source,
            **pool_stats,
        },
        "warmup": warmup,
        "sampled_parameters": sampled_parameters,
        "client_cpu_pct": round(client_cpu_pct, 2),
        "parallel_steps": parallel_steps,
    }


def _row(
    setting: str,
    value: Any,
    source: str,
    formula: str | None = None,
    bind: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {"setting": setting, "value": value, "source": source}
    if formula:
        row["formula"] = formula
    if bind:
        row["bind"] = bind
    return row
