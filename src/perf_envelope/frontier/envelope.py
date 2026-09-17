"""GREEN / AMBER / RED operating envelope."""

from __future__ import annotations

from typing import Any

import pandas as pd

from perf_envelope.experiment.refinement import classify


def build_envelope(
    frame: pd.DataFrame, slo_p95: float, green_fraction: float = 0.7
) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    grouped = (
        frame.groupby(
            [col for col in ["dataset_size", "selectivity", "concurrency", "cache_state"] if col in frame.columns],
            as_index=False,
        )["p95_ms"]
        .mean()
        .sort_values("dataset_size" if "dataset_size" in frame.columns else frame.columns[0])
    )
    rows = []
    for _, row in grouped.iterrows():
        payload = {k: (int(v) if k in {"dataset_size", "concurrency"} else v) for k, v in row.items()}
        payload["class"] = classify(float(row["p95_ms"]), slo_p95, green_fraction)
        payload["slo_p95_ms"] = slo_p95
        rows.append(payload)
    return rows


def safe_concurrency(envelope: list[dict[str, Any]]) -> int | None:
    green = [int(r["concurrency"]) for r in envelope if r.get("class") != "RED" and "concurrency" in r]
    return max(green) if green else None
