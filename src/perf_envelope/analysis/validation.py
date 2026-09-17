"""Hold-out-style error metrics and confidence tiers."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score


@dataclass
class ValidationReport:
    mae: float
    rmse: float
    mape: float
    r2: float
    confidence: str

    def as_dict(self) -> dict:
        return {
            "mae": self.mae,
            "rmse": self.rmse,
            "mape": self.mape,
            "r2": self.r2,
            "confidence": self.confidence,
        }


def mape(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    denom = np.clip(np.abs(y_true), 1e-6, None)
    return float(np.mean(np.abs((y_true - y_pred) / denom)) * 100)


def confidence_from_mape(value: float) -> str:
    if value <= 15:
        return "HIGH"
    if value <= 30:
        return "MEDIUM"
    return "LOW"


def validate_predictions(y_true: np.ndarray, y_pred: np.ndarray) -> ValidationReport:
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    mae = float(mean_absolute_error(y_true, y_pred))
    rmse = float(np.sqrt(mean_squared_error(y_true, y_pred)))
    mape_value = mape(y_true, y_pred)
    try:
        r2 = float(r2_score(y_true, y_pred))
    except Exception:  # noqa: BLE001
        r2 = float("nan")
    return ValidationReport(
        mae=mae,
        rmse=rmse,
        mape=mape_value,
        r2=r2,
        confidence=confidence_from_mape(mape_value),
    )
