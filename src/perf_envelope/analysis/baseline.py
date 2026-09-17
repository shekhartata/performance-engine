"""Interpretable polynomial/interaction regression (Model A)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import PolynomialFeatures, StandardScaler

from perf_envelope.analysis.features import feature_matrix


@dataclass
class BaselineFit:
    pipeline: Pipeline
    feature_names: list[str]
    coefficients: dict[str, float]

    def predict_p95(self, frame) -> np.ndarray:
        x, _, _ = feature_matrix(frame)
        log_pred = self.pipeline.predict(x)
        return np.expm1(np.clip(log_pred, 0, None))


def fit_baseline(frame, degree: int = 2) -> BaselineFit:
    x, y, names = feature_matrix(frame)
    pipeline = Pipeline(
        [
            ("scaler", StandardScaler()),
            ("poly", PolynomialFeatures(degree=degree, include_bias=True)),
            ("model", Ridge(alpha=1.0)),
        ]
    )
    pipeline.fit(x, y)
    poly: PolynomialFeatures = pipeline.named_steps["poly"]
    model: Ridge = pipeline.named_steps["model"]
    expanded = poly.get_feature_names_out(names)
    coefficients = {name: float(coef) for name, coef in zip(expanded, model.coef_)}
    coefficients["intercept"] = float(model.intercept_)
    return BaselineFit(pipeline=pipeline, feature_names=list(expanded), coefficients=coefficients)
