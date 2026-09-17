"""Extrapolate p95 curves to unmeasured dataset sizes."""

from __future__ import annotations

from typing import Any

import pandas as pd


def _template_row(frame: pd.DataFrame) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for column in (
        "selectivity",
        "concurrency",
        "cache_state",
        "document_size_bytes",
        "result_count",
        "n_returned",
        "model_id",
    ):
        if column not in frame.columns:
            continue
        series = frame[column]
        if pd.api.types.is_numeric_dtype(series):
            row[column] = float(series.median())
        else:
            mode = series.mode()
            row[column] = mode.iloc[0] if not mode.empty else series.iloc[0]
    row.setdefault("p95_ms", 0.0)
    return row


def measured_sizes(frame: pd.DataFrame) -> list[int]:
    if "dataset_size" not in frame.columns or frame.empty:
        return []
    return sorted({int(value) for value in frame["dataset_size"].dropna().tolist()})


def project_scales(
    frame: pd.DataFrame,
    project_documents: list[int],
    slo_p95: float,
    predictor,
) -> list[dict[str, Any]]:
    """Predict p95 at each requested scale. Unmeasured sizes are tagged extrapolated."""
    if not project_documents:
        return []
    measured = measured_sizes(frame)
    measured_range = [measured[0], measured[-1]] if measured else None
    template = _template_row(frame)
    rows: list[dict[str, Any]] = []
    for size in project_documents:
        candidate = dict(template)
        candidate["dataset_size"] = int(size)
        predicted = float(predictor.predict_p95(pd.DataFrame([candidate]))[0])
        rows.append(
            {
                "dataset_size": int(size),
                "predicted_p95_ms": predicted,
                "extrapolated": int(size) not in measured,
                "slo_pass": predicted <= slo_p95,
                "measured_range": measured_range,
            }
        )
    return rows


def per_scale_curves(frame: pd.DataFrame, slo_p95: float) -> list[dict[str, Any]]:
    """Summarize measured p95 vs concurrency / selectivity at each dataset size."""
    if frame.empty or "dataset_size" not in frame.columns:
        return []
    curves: list[dict[str, Any]] = []
    for size, subset in frame.groupby("dataset_size"):
        mean_p95 = float(subset["p95_ms"].mean()) if "p95_ms" in subset.columns else None
        vs_concurrency: list[dict[str, Any]] = []
        if "concurrency" in subset.columns and "p95_ms" in subset.columns:
            grouped = subset.groupby("concurrency")["p95_ms"].mean()
            vs_concurrency = [
                {"concurrency": int(conc), "p95_ms": float(value)}
                for conc, value in grouped.items()
            ]
        vs_selectivity: list[dict[str, Any]] = []
        if "selectivity" in subset.columns and "p95_ms" in subset.columns:
            grouped = subset.groupby("selectivity")["p95_ms"].mean()
            vs_selectivity = [
                {"selectivity": float(sel), "p95_ms": float(value)}
                for sel, value in grouped.items()
            ]
        max_conc = None
        passing = [row["concurrency"] for row in vs_concurrency if row["p95_ms"] <= slo_p95]
        if passing:
            max_conc = max(passing)
        curves.append(
            {
                "dataset_size": int(size),
                "p95_ms": mean_p95,
                "p95_vs_concurrency": vs_concurrency,
                "p95_vs_selectivity": vs_selectivity,
                "slo_pass": mean_p95 is not None and mean_p95 <= slo_p95,
                "max_concurrency_meeting_slo": max_conc,
                "extrapolated": False,
            }
        )
    curves.sort(key=lambda row: row["dataset_size"])
    return curves
