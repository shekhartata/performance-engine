"""SLO PASS→FAIL boundary search on the observed response surface."""

from __future__ import annotations

from typing import Any

import pandas as pd

from perf_envelope.experiment.refinement import classify


def detect_slo_boundary(frame: pd.DataFrame, slo_p95: float, green_fraction: float = 0.7) -> dict[str, Any]:
    if frame.empty:
        return {"status": "NO_DATA"}
    grouped = (
        frame.groupby("dataset_size", as_index=False)["p95_ms"].mean().sort_values("dataset_size")
    )
    last_pass = None
    first_fail = None
    for _, row in grouped.iterrows():
        cls = classify(float(row["p95_ms"]), slo_p95, green_fraction)
        if cls != "RED":
            last_pass = {"dataset_size": int(row["dataset_size"]), "p95_ms": float(row["p95_ms"]), "class": cls}
        elif first_fail is None:
            first_fail = {"dataset_size": int(row["dataset_size"]), "p95_ms": float(row["p95_ms"]), "class": cls}
            break
    if first_fail and last_pass:
        kind = "OBSERVED"
        estimate = (last_pass["dataset_size"] + first_fail["dataset_size"]) / 2
        return {
            "kind": kind,
            "last_pass": last_pass,
            "first_fail": first_fail,
            "estimated_breakpoint_documents": estimate,
            "safe_region_documents": last_pass["dataset_size"],
        }
    if first_fail and not last_pass:
        return {
            "kind": "OBSERVED",
            "last_pass": None,
            "first_fail": first_fail,
            "estimated_breakpoint_documents": first_fail["dataset_size"],
            "safe_region_documents": None,
        }
    max_n = int(grouped["dataset_size"].max())
    return {
        "kind": "EXTRAPOLATED",
        "last_pass": {
            "dataset_size": max_n,
            "p95_ms": float(grouped.iloc[-1]["p95_ms"]),
            "class": classify(float(grouped.iloc[-1]["p95_ms"]), slo_p95, green_fraction),
        },
        "first_fail": None,
        "estimated_breakpoint_documents": None,
        "safe_region_documents": max_n,
        "note": "No SLO violation observed in tested range",
    }
