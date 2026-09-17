"""Deterministic slope-ratio change-point detector."""

from __future__ import annotations

from typing import Any

import pandas as pd


def detect_change_points(
    frame: pd.DataFrame,
    *,
    variable: str = "dataset_size",
    metric: str = "p95_ms",
    multiplier: float = 2.0,
) -> list[dict[str, Any]]:
    if variable not in frame.columns:
        return []
    grouped = frame.groupby(variable, as_index=False)[metric].mean().sort_values(variable)
    if len(grouped) < 3:
        return []
    xs = grouped[variable].to_numpy(dtype=float)
    ys = grouped[metric].to_numpy(dtype=float)
    slopes = []
    for i in range(1, len(xs)):
        dx = xs[i] - xs[i - 1]
        dy = ys[i] - ys[i - 1]
        slopes.append(dy / dx if dx else 0.0)
    flags = []
    for i in range(1, len(slopes)):
        prev = slopes[i - 1]
        cur = slopes[i]
        if prev <= 0:
            continue
        if cur > prev * multiplier:
            flags.append(
                {
                    "variable": variable,
                    "from": float(xs[i]),
                    "to": float(xs[i + 1]),
                    "metric_from": float(ys[i]),
                    "metric_to": float(ys[i + 1]),
                    "previous_slope": prev,
                    "slope": cur,
                    "kind": "OBSERVED",
                    "note": f"performance inflection approximately {int(xs[i])}–{int(xs[i + 1])}",
                }
            )
    return flags
