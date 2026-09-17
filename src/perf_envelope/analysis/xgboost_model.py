"""Primary gradient-boosted predictor targeting log1p(p95_latency_ms).

XGBoost is preferred when the native library loads. On macOS without libomp the
engine falls back to sklearn's GradientBoostingRegressor so analysis still runs.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sklearn.ensemble import GradientBoostingRegressor

from perf_envelope.analysis.features import FEATURE_COLUMNS, feature_matrix

try:
    from xgboost import XGBRegressor
except Exception:  # noqa: BLE001 — missing OpenMP / native lib
    XGBRegressor = None  # type: ignore[misc, assignment]


@dataclass
class BoostedFit:
    model: object
    feature_names: list[str]
    importances: dict[str, float]
    backend: str

    def predict_p95(self, frame) -> np.ndarray:
        x, _, _ = feature_matrix(frame)
        log_pred = self.model.predict(x)
        return np.expm1(np.clip(log_pred, 0, None))


def fit_xgboost(frame, random_state: int = 42) -> BoostedFit | None:
    x, y, names = feature_matrix(frame)
    if len(y) < 4:
        return None
    if XGBRegressor is not None:
        n_estimators = 80 if len(y) >= 20 else 40
        max_depth = 3 if len(y) < 30 else 4
        model = XGBRegressor(
            n_estimators=n_estimators,
            max_depth=max_depth,
            learning_rate=0.1,
            subsample=0.9,
            colsample_bytree=0.9,
            objective="reg:squarederror",
            random_state=random_state,
            n_jobs=1,
            verbosity=0,
        )
        model.fit(x, y)
        importances = {
            name: float(value) for name, value in zip(names, model.feature_importances_)
        }
        return BoostedFit(
            model=model, feature_names=FEATURE_COLUMNS, importances=importances, backend="xgboost"
        )

    model = GradientBoostingRegressor(
        random_state=random_state,
        n_estimators=80 if len(y) >= 20 else 40,
        max_depth=2,
        learning_rate=0.1,
    )
    model.fit(x, y)
    importances = {
        name: float(value) for name, value in zip(names, model.feature_importances_)
    }
    return BoostedFit(
        model=model,
        feature_names=FEATURE_COLUMNS,
        importances=importances,
        backend="sklearn_gbrt",
    )
