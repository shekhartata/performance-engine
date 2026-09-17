"""Evidence-backed diagnostic observations. No absolute causal claims."""

from __future__ import annotations

from typing import Any

import pandas as pd


def collect_diagnostics(frame: pd.DataFrame) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    if frame.empty:
        return diagnostics

    if {"docs_examined_per_returned", "dataset_size"}.issubset(frame.columns):
        ordered = frame.sort_values("dataset_size")
        start = float(ordered["docs_examined_per_returned"].iloc[0] or 0)
        end = float(ordered["docs_examined_per_returned"].iloc[-1] or 0)
        if end > start * 1.5 and end > 2:
            diagnostics.append(
                {
                    "observation": "docsExamined/nReturned increasing",
                    "evidence": f"ratio rose from {start:.2f} to {end:.2f} as dataset size increased",
                    "hypothesis": "query scan amplification",
                    "confidence": "MEDIUM",
                }
            )

    if {"keys_examined_per_returned", "dataset_size"}.issubset(frame.columns):
        ordered = frame.sort_values("dataset_size")
        start = float(ordered["keys_examined_per_returned"].iloc[0] or 0)
        end = float(ordered["keys_examined_per_returned"].iloc[-1] or 0)
        if end > start * 1.5 and end > 2:
            diagnostics.append(
                {
                    "observation": "keysExamined/nReturned increasing",
                    "evidence": f"ratio rose from {start:.2f} to {end:.2f} as dataset size increased",
                    "hypothesis": "index scan amplification",
                    "confidence": "MEDIUM",
                }
            )

    if "cache_state" in frame.columns and "p95_ms" in frame.columns:
        hot = frame[frame["cache_state"].astype(str).str.contains("hot")]["p95_ms"]
        cold = frame[frame["cache_state"].astype(str).str.contains("cold")]["p95_ms"]
        if len(hot) and len(cold) and cold.mean() > hot.mean() * 1.3:
            diagnostics.append(
                {
                    "observation": "latency rises with cold workload",
                    "evidence": f"mean p95 cold={cold.mean():.1f}ms vs hot={hot.mean():.1f}ms",
                    "hypothesis": "cache/storage sensitivity",
                    "confidence": "MEDIUM",
                }
            )

    if "concurrency" in frame.columns:
        grouped = frame.groupby("concurrency")["p95_ms"].mean()
        if 1 in grouped.index:
            low = grouped.loc[1]
            high_idx = grouped.index.max()
            high = grouped.loc[high_idx]
            if high_idx >= 8 and high > low * 1.5:
                diagnostics.append(
                    {
                        "observation": f"latency stable at concurrency=1, sharply increases at concurrency={int(high_idx)}",
                        "evidence": f"p95 {low:.1f}ms @1 vs {high:.1f}ms @{int(high_idx)}",
                        "hypothesis": "concurrency/resource saturation",
                        "confidence": "MEDIUM",
                    }
                )
    return diagnostics
