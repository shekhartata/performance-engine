"""Split the variation in p95 across the settings that were varied.

On a complete, balanced grid the split is exact and model-free: each setting's
main effect and each pair's interaction are sums of squares of log p95. When the
grid is incomplete, fall back to shuffling one setting at a time and measuring
how much the prediction model's error grows. Neither is causal proof.
"""

from __future__ import annotations

import itertools
from math import prod
from typing import Any

import numpy as np
import pandas as pd

from perf_envelope.analysis.features import FeatureSpec, with_plan
from perf_envelope.analysis.sensitivity import friendly_name

MEASURED_OUTPUTS = {"result_count": "Documents per request"}


def _pct(value: float, total: float) -> float:
    return round(100.0 * max(value, 0.0) / total, 1) if total > 0 else 0.0


def _complete_grid(frame: pd.DataFrame, factors: list[str]) -> tuple[bool, int]:
    counts = frame.groupby(factors, dropna=False).size()
    levels = prod(frame[name].nunique(dropna=False) for name in factors)
    balanced = counts.nunique() == 1
    return bool(len(counts) == levels and balanced), int(counts.iloc[0]) if balanced else 0


def _variance_split(frame: pd.DataFrame, factors: list[str], repeats: int) -> dict[str, Any]:
    y = frame["_y"]
    grand = y.mean()
    total = float(((y - grand) ** 2).sum())
    main: dict[str, float] = {}
    for name in factors:
        means = frame.groupby(name, dropna=False)["_y"].transform("mean")
        main[name] = float(((means - grand) ** 2).sum())
    pairs: dict[tuple[str, str], float] = {}
    for left, right in itertools.combinations(factors, 2):
        means = frame.groupby([left, right], dropna=False)["_y"].transform("mean")
        pairs[(left, right)] = float(((means - grand) ** 2).sum()) - main[left] - main[right]
    noise = None
    if repeats > 1:
        cell_means = frame.groupby(factors, dropna=False)["_y"].transform("mean")
        noise = float(((y - cell_means) ** 2).sum())
    higher = total - sum(main.values()) - sum(pairs.values()) - (noise or 0.0)
    return {
        "method": "variance_split",
        "settings": {friendly_name(k): _pct(v, total) for k, v in sorted(main.items(), key=lambda kv: -kv[1])},
        "pairs": {
            f"{friendly_name(a)} × {friendly_name(b)}": _pct(v, total)
            for (a, b), v in sorted(pairs.items(), key=lambda kv: -kv[1])
        },
        "higher_order": _pct(higher, total) if len(factors) > 2 or repeats > 1 else None,
        "noise": None if noise is None else _pct(noise, total),
    }


def _permutation(frame: pd.DataFrame, factors: list[str], predictor, seed: int, rounds: int) -> dict[str, Any]:
    y = frame["_y"].to_numpy()

    def error(candidate: pd.DataFrame) -> float:
        pred = np.log1p(np.asarray(predictor.predict_p95(candidate), dtype=float))
        return float(np.mean((pred - y) ** 2))

    base = error(frame)
    rng = np.random.default_rng(seed)
    gains: dict[str, float] = {}
    for name in factors:
        losses = []
        for _ in range(rounds):
            shuffled = frame.copy()
            shuffled[name] = rng.permutation(shuffled[name].to_numpy())
            losses.append(error(shuffled))
        gains[name] = max(float(np.mean(losses)) - base, 0.0)
    total = sum(gains.values())
    return {
        "method": "permutation",
        "settings": {friendly_name(k): _pct(v, total) for k, v in sorted(gains.items(), key=lambda kv: -kv[1])},
        "pairs": {},
        "higher_order": None,
        "noise": None,
    }


def measured_outputs(frame: pd.DataFrame, factors: list[str]) -> list[dict[str, Any]]:
    """Describe measured quantities that are left out of the ranking, and why."""
    rows = []
    for column, label in MEASURED_OUTPUTS.items():
        if column not in frame.columns:
            continue
        values = pd.to_numeric(frame[column], errors="coerce")
        if values.dropna().nunique() <= 1:
            continue
        determined = bool(
            factors and frame.assign(_v=values).groupby(factors, dropna=False)["_v"].nunique().max() <= 1
        )
        rho = values.rank().corr(pd.to_numeric(frame["p95_ms"], errors="coerce").rank())
        rows.append(
            {
                "name": label,
                "column": column,
                "min": float(values.min()),
                "max": float(values.max()),
                "determined_by_settings": determined,
                "rank_correlation_with_p95": None if pd.isna(rho) else round(float(rho), 2),
            }
        )
    return rows


def attribute(
    frame: pd.DataFrame,
    spec: FeatureSpec,
    predictor=None,
    seed: int = 42,
    rounds: int = 20,
) -> dict[str, Any]:
    working = with_plan(frame).copy()
    working["_y"] = np.log1p(pd.to_numeric(working["p95_ms"], errors="coerce").clip(lower=0))
    working = working[working["_y"].notna()]
    factors = [name for name in spec.factors if name in working.columns]
    base = {
        "held_fixed": {friendly_name(k): v for k, v in spec.held_fixed.items()},
        "measured_outputs": measured_outputs(working, factors),
        "rows": int(len(working)),
    }
    if not factors or working.empty:
        return {**base, "method": "none", "settings": {}, "pairs": {}, "higher_order": None, "noise": None,
                "complete_grid": False}
    complete, repeats = _complete_grid(working, factors)
    if complete:
        result = _variance_split(working, factors, repeats)
    elif predictor is not None:
        result = _permutation(working, factors, predictor, seed, rounds)
    else:
        result = {"method": "none", "settings": {}, "pairs": {}, "higher_order": None, "noise": None}
    return {**base, **result, "complete_grid": complete, "repetitions_per_cell": repeats or None}
