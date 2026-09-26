"""Interpretable polynomial/interaction regression (Model A)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import PolynomialFeatures, StandardScaler

from perf_envelope.analysis.features import FeatureSpec, build_feature_spec, design, target


@dataclass
class BaselineFit:
    pipeline: Pipeline
    feature_names: list[str]
    coefficients: dict[str, float]
    spec: FeatureSpec

    def predict_p95(self, frame) -> np.ndarray:
        log_pred = self.pipeline.predict(design(frame, self.spec))
        return np.expm1(np.clip(log_pred, 0, None))


def fit_baseline(
    frame, degree: int = 2, extras: list[str] | None = None, spec: FeatureSpec | None = None
) -> BaselineFit:
    spec = spec or build_feature_spec(frame, extras)
    x, y, names = design(frame, spec), target(frame), spec.names
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
    return BaselineFit(
        pipeline=pipeline,
        feature_names=list(expanded),
        coefficients=coefficients,
        spec=spec,
    )
