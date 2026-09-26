"""Largest tested value of a sweep axis that still meets the SLO."""

from __future__ import annotations

from typing import Any

import pandas as pd

from perf_envelope.experiment.refinement import classify

# Spec names that differ from observation column names.
COLUMN_ALIASES = {"documents": "dataset_size", "model": "plan", "model_id": "plan"}


def find_thresholds(
    frame: pd.DataFrame,
    axis: str,
    slo_p95: float,
    for_each: list[str] | None = None,
    green_fraction: float = 0.7,
) -> list[dict[str, Any]]:
    """Max axis value at or under the SLO, before the first failing value, per group."""
    if frame.empty or axis not in frame.columns:
        return []
    working = frame.copy()
    if "plan" not in working.columns and "model_id" in working.columns:
        working["plan"] = working["model_id"]
    group_cols: list[str] = []
    for name in for_each or []:
        column = COLUMN_ALIASES.get(name, name)
        if column in working.columns and column != axis and column not in group_cols:
            group_cols.append(column)
    if "plan" in working.columns and working["plan"].nunique() > 1 and "plan" not in group_cols and axis != "plan":
        group_cols.append("plan")

    rows: list[dict[str, Any]] = []
    if group_cols:
        groups = working.groupby(group_cols, dropna=False)
    else:
        groups = [((), working)]

    for key, subset in groups:
        if not isinstance(key, tuple):
            key = (key,)
        grouped = subset.groupby(axis, dropna=False)["p95_ms"].mean().sort_index()
        max_meeting = None
        first_fail = None
        curve = []
        for value, p95 in grouped.items():
            label = classify(float(p95), slo_p95, green_fraction)
            point = {"value": _jsonable(value), "p95_ms": float(p95), "class": label}
            curve.append(point)
            if label == "RED" and first_fail is None:
                first_fail = point
                break
            max_meeting = point
        record: dict[str, Any] = {
            "axis": axis,
            "max_meeting_slo": None if max_meeting is None else max_meeting["value"],
            "max_meeting_p95_ms": None if max_meeting is None else max_meeting["p95_ms"],
            "first_fail": None if first_fail is None else first_fail["value"],
            "curve": curve,
        }
        for name, value in zip(group_cols, key):
            record[name] = _jsonable(value)
        rows.append(record)
    return rows


def _jsonable(value: Any) -> Any:
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:  # noqa: BLE001
            return value
    return value
