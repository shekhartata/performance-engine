"""Compare candidate data models under the same experiment dimensions."""

from __future__ import annotations

from typing import Any

import pandas as pd

from perf_envelope.experiment.refinement import classify
from perf_envelope.frontier.envelope import safe_concurrency
from perf_envelope.frontier.slo_boundary import detect_slo_boundary


def compare_models(frame: pd.DataFrame, slo_p95: float, analysis: dict[str, Any] | None = None) -> dict[str, Any]:
    if "model_id" not in frame.columns:
        return {"models": [], "rows": []}
    models = list(frame["model_id"].unique())
    sizes = sorted(frame["dataset_size"].unique()) if "dataset_size" in frame.columns else []
    rows = []
    for size in sizes:
        row = {"dataset_size": int(size)}
        for model in models:
            subset = frame[(frame["model_id"] == model) & (frame["dataset_size"] == size)]
            row[f"{model}_p95_ms"] = float(subset["p95_ms"].mean()) if len(subset) else None
        rows.append(row)
    summary = {}
    per_model = (analysis or {}).get("per_model", {})
    for model in models:
        subset = frame[frame["model_id"] == model]
        boundary = per_model.get(str(model), {}).get("slo_boundary") or detect_slo_boundary(subset, slo_p95)
        envelope = per_model.get(str(model), {}).get("envelope") or []
        summary[str(model)] = {
            "first_slo_violation": (boundary.get("first_fail") or {}).get("dataset_size")
            if boundary
            else None,
            "safe_region_documents": boundary.get("safe_region_documents") if boundary else None,
            "safe_concurrency": safe_concurrency(envelope),
            "mean_p95_ms": float(subset["p95_ms"].mean()) if len(subset) else None,
        }
    return {"models": [str(m) for m in models], "p95_by_size": rows, "summary": summary}
