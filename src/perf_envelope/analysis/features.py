"""Model inputs: only the settings that were varied in the run, plus plan.

Measured quantities such as result count or documents examined are outcomes of
the settings, not settings themselves, so they are never model inputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

BUILTIN_NUMERIC = ["dataset_size", "selectivity", "concurrency", "document_size_bytes"]
BUILTIN_CATEGORICAL = ["cache_state"]
PLAN = "plan"
CONSTANT = "constant"


@dataclass(frozen=True)
class FeatureSpec:
    numeric: tuple[str, ...]
    categorical: tuple[tuple[str, tuple[str, ...]], ...]
    fill: dict[str, Any] = field(default_factory=dict)
    held_fixed: dict[str, Any] = field(default_factory=dict)

    @property
    def factors(self) -> list[str]:
        return [name for name in self.numeric if name != CONSTANT] + [
            name for name, _ in self.categorical
        ]

    @property
    def names(self) -> list[str]:
        numeric = [CONSTANT if name == CONSTANT else f"log_{name}" for name in self.numeric]
        categorical = [f"{name}__{level}" for name, levels in self.categorical for level in levels]
        return numeric + categorical


def with_plan(frame: pd.DataFrame) -> pd.DataFrame:
    if PLAN in frame.columns or "model_id" not in frame.columns:
        return frame
    out = frame.copy()
    out[PLAN] = out["model_id"]
    return out


def _is_numeric(series: pd.Series) -> bool:
    values = pd.to_numeric(series, errors="coerce")
    return bool(values.notna().mean() >= 0.8)


def _scalar(value: Any) -> Any:
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:  # noqa: BLE001
            return value
    return value


def build_feature_spec(frame: pd.DataFrame, extras: list[str] | None = None) -> FeatureSpec:
    working = with_plan(frame)
    numeric_candidates = list(BUILTIN_NUMERIC)
    categorical_candidates = list(BUILTIN_CATEGORICAL)
    for name in extras or []:
        if name in numeric_candidates or name in categorical_candidates or name not in working.columns:
            continue
        (numeric_candidates if _is_numeric(working[name]) else categorical_candidates).append(name)
    categorical_candidates.append(PLAN)

    numeric: list[str] = []
    categorical: list[tuple[str, tuple[str, ...]]] = []
    fill: dict[str, Any] = {}
    held_fixed: dict[str, Any] = {}
    for name in numeric_candidates:
        if name not in working.columns:
            continue
        values = pd.to_numeric(working[name], errors="coerce").dropna()
        if values.empty:
            continue
        if values.nunique() > 1:
            numeric.append(name)
            fill[name] = float(values.median())
        elif name != "document_size_bytes" or values.iloc[0] > 0:
            # document_size_bytes is 0 when existing data was not sized.
            held_fixed[name] = _scalar(values.iloc[0])
    for name in categorical_candidates:
        if name not in working.columns:
            continue
        values = working[name].dropna().astype(str)
        if values.empty:
            continue
        levels = tuple(sorted(values.unique()))
        if len(levels) > 1:
            categorical.append((name, levels))
            fill[name] = values.mode().iloc[0]
        elif name != PLAN:
            held_fixed[name] = levels[0]
    if not numeric and not categorical:
        numeric.append(CONSTANT)
    return FeatureSpec(tuple(numeric), tuple(categorical), fill, held_fixed)


def design(frame: pd.DataFrame, spec: FeatureSpec) -> np.ndarray:
    working = with_plan(frame)
    columns: list[np.ndarray] = []
    for name in spec.numeric:
        if name == CONSTANT:
            columns.append(np.zeros(len(working)))
            continue
        raw = working[name] if name in working.columns else pd.Series([np.nan] * len(working))
        values = pd.to_numeric(raw, errors="coerce").fillna(spec.fill.get(name, 0.0))
        columns.append(np.log1p(values.clip(lower=0).to_numpy(dtype=float)))
    for name, levels in spec.categorical:
        raw = working[name] if name in working.columns else pd.Series([None] * len(working))
        values = raw.fillna(spec.fill.get(name, levels[0])).astype(str).to_numpy()
        for level in levels:
            columns.append((values == level).astype(float))
    return np.column_stack(columns) if columns else np.zeros((len(working), 0))


def target(frame: pd.DataFrame) -> np.ndarray:
    return np.log1p(pd.to_numeric(frame["p95_ms"], errors="coerce").fillna(0).clip(lower=0)).to_numpy(
        dtype=float
    )


def feature_matrix(
    frame: pd.DataFrame, extras: list[str] | None = None, spec: FeatureSpec | None = None
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    spec = spec or build_feature_spec(frame, extras)
    return design(frame, spec), target(frame), spec.names
