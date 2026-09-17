"""Feature transforms for the regression models."""

from __future__ import annotations

import numpy as np
import pandas as pd

from perf_envelope.workload.cache import HOT

FEATURE_COLUMNS = [
    "log_dataset_size",
    "log_selectivity",
    "log_concurrency",
    "cache_state_hot",
    "log_document_size",
    "log_result_cardinality",
]


def _log1p_series(frame: pd.DataFrame, column: str, default: float = 1.0) -> np.ndarray:
    values = frame[column] if column in frame.columns else default
    return np.log1p(pd.to_numeric(values, errors="coerce").fillna(default).clip(lower=0))


def transform(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["log_dataset_size"] = _log1p_series(out, "dataset_size")
    out["log_selectivity"] = _log1p_series(out, "selectivity", default=0.01)
    out["log_concurrency"] = _log1p_series(out, "concurrency")
    cache = out["cache_state"] if "cache_state" in out.columns else HOT
    out["cache_state_hot"] = (
        pd.Series(cache).astype(str).str.contains("hot").astype(float)
    )
    out["log_document_size"] = _log1p_series(out, "document_size_bytes", default=1.0)
    result_col = "result_count" if "result_count" in out.columns else "n_returned"
    out["log_result_cardinality"] = _log1p_series(out, result_col, default=1.0)
    out["y_log_p95"] = np.log1p(pd.to_numeric(out["p95_ms"], errors="coerce").fillna(0).clip(lower=0))
    return out


def feature_matrix(frame: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, list[str]]:
    transformed = transform(frame)
    x = transformed[FEATURE_COLUMNS].to_numpy(dtype=float)
    y = transformed["y_log_p95"].to_numpy(dtype=float)
    return x, y, FEATURE_COLUMNS
