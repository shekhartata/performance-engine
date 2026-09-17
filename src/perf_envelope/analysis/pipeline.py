"""Fit both predictors and persist analysis artifacts."""

from __future__ import annotations

from typing import Any

import pandas as pd

from perf_envelope.analysis.baseline import fit_baseline
from perf_envelope.analysis.projection import per_scale_curves, project_scales
from perf_envelope.analysis.sensitivity import relative_sensitivity
from perf_envelope.analysis.validation import validate_predictions
from perf_envelope.analysis.xgboost_model import fit_xgboost
from perf_envelope.diagnostics.heuristics import collect_diagnostics
from perf_envelope.frontier.change_point import detect_change_points
from perf_envelope.frontier.envelope import build_envelope
from perf_envelope.frontier.slo_boundary import detect_slo_boundary
from perf_envelope.storage.runs import ExperimentRepository


def analyze_frame(
    frame: pd.DataFrame,
    slo_p95: float,
    green_fraction: float = 0.7,
    project_documents: list[int] | None = None,
) -> dict[str, Any]:
    if frame.empty:
        raise ValueError("No observations to analyze")
    baseline = fit_baseline(frame)
    boosted = fit_xgboost(frame)
    predictor = boosted if boosted is not None else baseline
    predicted = predictor.predict_p95(frame)
    actual = frame["p95_ms"].to_numpy(dtype=float)
    validation = validate_predictions(actual, predicted)
    annotated = frame.copy()
    annotated["predicted_p95_ms"] = predicted
    models = sorted(annotated["model_id"].unique()) if "model_id" in annotated.columns else ["default"]
    per_model = {}
    for model in models:
        subset = annotated[annotated["model_id"] == model] if "model_id" in annotated.columns else annotated
        per_model[str(model)] = {
            "slo_boundary": detect_slo_boundary(subset, slo_p95),
            "change_points": detect_change_points(subset),
            "envelope": build_envelope(subset, slo_p95, green_fraction),
            "per_scale": per_scale_curves(subset, slo_p95),
            "projection": project_scales(subset, project_documents or [], slo_p95, predictor),
        }
    return {
        "validation": validation.as_dict(),
        "baseline_coefficients": baseline.coefficients,
        "xgboost_importances": boosted.importances if boosted else {},
        "sensitivity": relative_sensitivity(boosted),
        "per_model": per_model,
        "per_scale": per_scale_curves(annotated, slo_p95),
        "projection": project_scales(annotated, project_documents or [], slo_p95, predictor),
        "diagnostics": collect_diagnostics(annotated),
        "row_count": int(len(frame)),
        "used_xgboost": boosted is not None and getattr(boosted, "backend", "") == "xgboost",
        "boosting_backend": getattr(boosted, "backend", None),
        "predictions": annotated[
            [
                col
                for col in [
                    "model_id",
                    "dataset_size",
                    "selectivity",
                    "concurrency",
                    "cache_state",
                    "p95_ms",
                    "predicted_p95_ms",
                ]
                if col in annotated.columns
            ]
        ].to_dict(orient="records"),
    }


def analyze_run(
    run_dir,
    slo_p95: float,
    green_fraction: float = 0.7,
    repo: ExperimentRepository | None = None,
) -> dict[str, Any]:
    repo = repo or ExperimentRepository()
    frame = repo.load_observations(run_dir)
    experiment = {}
    try:
        experiment = repo.read_json(run_dir, "configuration/experiment.json")
    except Exception:  # noqa: BLE001
        experiment = {}
    project_documents = list(experiment.get("project_documents") or []) if isinstance(experiment, dict) else []
    analysis = analyze_frame(
        frame, slo_p95, green_fraction, project_documents=project_documents
    )
    repo.write_json(run_dir, "analysis.json", analysis)
    return analysis
