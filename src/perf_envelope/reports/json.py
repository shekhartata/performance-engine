"""Assemble the canonical V1 report payload and write JSON/Markdown/HTML."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape

from perf_envelope.comparison.comparator import compare_models
from perf_envelope.reports.plots import (
    plot_envelope,
    plot_observed_vs_predicted,
    plot_p95_curves_by_scale,
    plot_p95_vs,
)
from perf_envelope.storage.runs import ExperimentRepository

TEMPLATE_DIR = Path(__file__).parent / "templates"


def confidence_label(analysis: dict[str, Any]) -> str:
    validation = analysis.get("validation") or {}
    per_model = analysis.get("per_model") or {}
    observed = any(
        (payload.get("slo_boundary") or {}).get("kind") == "OBSERVED"
        and (payload.get("slo_boundary") or {}).get("first_fail")
        for payload in per_model.values()
    )
    mape = validation.get("mape", 999)
    if observed and mape <= 15:
        return "HIGH"
    if mape <= 30:
        return "MEDIUM"
    return "LOW"


def build_report(
    run_dir: Path,
    frame: pd.DataFrame,
    analysis: dict[str, Any],
    *,
    slo: dict[str, Any] | None = None,
    environment: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    experiment: dict[str, Any] | None = None,
    run_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    slo = slo or {}
    slo_p95 = (slo.get("latency") or {}).get("p95_ms") or 100
    comparison = compare_models(frame, slo_p95, analysis)
    plots_dir = run_dir / "plots"
    plots = {
        "p95_vs_dataset_size": plot_p95_vs(frame, "dataset_size", plots_dir / "p95_vs_n.png"),
        "p95_vs_concurrency": plot_p95_vs(frame, "concurrency", plots_dir / "p95_vs_concurrency.png"),
        "p95_vs_selectivity": plot_p95_vs(frame, "selectivity", plots_dir / "p95_vs_selectivity.png"),
        "observed_vs_predicted": plot_observed_vs_predicted(
            analysis.get("predictions") or [], plots_dir / "observed_vs_predicted.png"
        ),
        "curves_by_scale": plot_p95_curves_by_scale(frame, plots_dir / "curves_by_scale.png"),
    }
    if "union_count" in frame.columns:
        plots["p95_vs_union_count"] = plot_p95_vs(
            frame, "union_count", plots_dir / "p95_vs_union_count.png"
        )
        plots["union_by_scale"] = plot_p95_curves_by_scale(
            frame, plots_dir / "union_by_scale.png", x="union_count"
        )
    first_model = next(iter((analysis.get("per_model") or {}).values()), {})
    plots["envelope"] = plot_envelope(first_model.get("envelope") or [], plots_dir / "envelope.png")
    settings = run_settings or {}
    limitations = [
        "Results apply only to the tested Atlas cluster, data model, query shape and workload.",
        "Cache residency is estimated (estimated_hot / estimated_cold), not guaranteed.",
        "The engine does not recommend indexes or rewrite queries.",
    ]
    if settings:
        limitations.append(
            "Connection pool size is listed per test case under Run settings. "
            "Derived pools use concurrency × parallel steps + headroom."
        )
    if any(int(cell.get("parallel_steps") or 1) > 1 for cell in settings.get("cells") or []):
        limitations.append(
            "Application fan-out timings include this machine's round-trip time to Atlas. "
            "A client in the same region would pay less network time per branch."
        )
    measured_columns = [
        "model_id",
        "plan",
        "dataset_size",
        "selectivity",
        "concurrency",
        "cache_state",
        "union_count",
        "matches_per_key",
        "p50_ms",
        "p95_ms",
        "p99_ms",
        "qps",
        "error_rate",
        "result_count",
        "pool_max_size",
        "pool_wait_p95_ms",
        "parallel_steps",
        "client_cpu_pct",
    ]
    return {
        "executive_summary": {
            "run_id": run_dir.name,
            "models": comparison.get("models"),
            "confidence": confidence_label(analysis),
            "validation": analysis.get("validation"),
            "sensitivity": analysis.get("sensitivity"),
        },
        "environment": environment or {},
        "data_model": list(frame["model_id"].unique()) if "model_id" in frame.columns else [],
        "indexes": {},
        "access_pattern": query or {},
        "dataset_characteristics": {
            "sizes": sorted(frame["dataset_size"].unique().tolist()) if "dataset_size" in frame.columns else [],
        },
        "experiment_dimensions": experiment.get("dimensions") if experiment else {},
        "slo": slo,
        "measured_performance": frame[
            [col for col in measured_columns if col in frame.columns]
        ].to_dict(orient="records"),
        "performance_surface": analysis.get("predictions"),
        "observed_boundary": {
            model: payload.get("slo_boundary")
            for model, payload in (analysis.get("per_model") or {}).items()
        },
        "change_points": {
            model: payload.get("change_points")
            for model, payload in (analysis.get("per_model") or {}).items()
        },
        "sensitivity_analysis": analysis.get("sensitivity"),
        "diagnostics": analysis.get("diagnostics"),
        "confidence": confidence_label(analysis),
        "model_comparison": comparison,
        "run_settings": settings,
        "pool_lines": _pool_lines(settings),
        "thresholds": analysis.get("thresholds") or [],
        "limitations": limitations,
        "raw_results_path": str(run_dir / "observations.parquet"),
        "plots": plots,
        "scale_curves": analysis.get("per_scale") or [],
        "projection": analysis.get("projection") or [],
        "envelope": {
            model: payload.get("envelope")
            for model, payload in (analysis.get("per_model") or {}).items()
        },
    }


def write_json_report(run_dir: Path, report: dict[str, Any]) -> Path:
    path = run_dir / "report.json"
    serializable = dict(report)
    serializable["plots"] = {k: ("<embedded png>" if v else None) for k, v in (report.get("plots") or {}).items()}
    path.write_text(json.dumps(serializable, indent=2, default=str))
    return path


def write_markdown_report(run_dir: Path, report: dict[str, Any]) -> Path:
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=False)
    template = env.get_template("report.md.j2")
    path = run_dir / "report.md"
    path.write_text(template.render(report=report))
    return path


def write_html_report(run_dir: Path, report: dict[str, Any]) -> Path:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("report.html.j2")
    path = run_dir / "report.html"
    path.write_text(template.render(report=report))
    return path


def generate_reports(run_dir: Path, repo: ExperimentRepository | None = None) -> dict[str, Path]:
    repo = repo or ExperimentRepository()
    frame = repo.load_observations(run_dir)
    analysis = repo.read_json(run_dir, "analysis.json")
    environment = _optional_json(run_dir / "environment.json")
    query = _optional_json(run_dir / "configuration" / "query.json")
    experiment = _optional_json(run_dir / "configuration" / "experiment.json")
    slo = experiment.get("slo") if isinstance(experiment.get("slo"), dict) else {}
    slo_file = run_dir / "configuration" / "slo.json"
    if slo_file.exists():
        slo = json.loads(slo_file.read_text())
    run_settings = _optional_json(run_dir / "configuration" / "run_settings.json")
    report = build_report(
        run_dir,
        frame,
        analysis,
        slo=slo,
        environment=environment,
        query=query,
        experiment=experiment,
        run_settings=run_settings or None,
    )
    return {
        "json": write_json_report(run_dir, report),
        "markdown": write_markdown_report(run_dir, report),
        "html": write_html_report(run_dir, report),
    }


def _pool_lines(settings: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for cell in settings.get("cells") or []:
        pool = cell.get("pool") or {}
        wait = pool.get("wait_p95_ms")
        wait_text = f"{float(wait):.3f}" if isinstance(wait, (int, float)) else "n/a"
        lines.append(
            f"{cell.get('model')} repetition {cell.get('repetition', 0)}: "
            f"Connection pool: {pool.get('formula')}, "
            f"peak in use {pool.get('peak_in_use')}, pool wait p95 {wait_text} ms."
        )
    return lines


def _optional_json(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text())
    return {}
