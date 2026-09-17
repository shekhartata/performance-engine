"""Per-request query parameter generation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from pymongo.collection import Collection

from perf_envelope.config.models import ParameterSpec
from perf_envelope.query.parser import apply_range_duration


class QueryParameterGenerator:
    def __init__(
        self,
        specs: dict[str, ParameterSpec],
        rng: np.random.Generator | None = None,
        cached_values: dict[str, list[Any]] | None = None,
    ):
        self.specs = specs
        self.rng = rng or np.random.default_rng()
        self.cached_values = cached_values or {}

    def preload(self, collection: Collection, field: str, limit: int = 500) -> None:
        values = collection.distinct(field)
        if len(values) > limit:
            idx = self.rng.choice(len(values), size=limit, replace=False)
            values = [values[i] for i in idx]
        self.cached_values[field] = list(values)

    def next(self, selectivity: float | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        for name, spec in self.specs.items():
            params[name] = self._value(name, spec, selectivity)
        return params

    def _value(self, name: str, spec: ParameterSpec, selectivity: float | None) -> Any:
        strategy = spec.strategy
        if strategy == "fixed" or spec.type == "literal":
            return apply_range_duration(spec.value)
        if spec.type == "datetime_range" or strategy == "range":
            return self._range_value(spec, selectivity)
        values = self.cached_values.get(spec.field or name, [])
        if not values:
            if spec.value is not None:
                return spec.value
            return 1
        if strategy == "hot_value":
            return values[0]
        if strategy == "cold_value":
            return values[-1]
        if strategy == "percentile":
            idx = int(np.clip(int((selectivity or 0.5) * (len(values) - 1)), 0, len(values) - 1))
            return values[idx]
        if strategy == "selectivity_targeted" and selectivity is not None and len(values) > 1:
            # Higher selectivity -> more common (earlier zipf) values.
            rank = int(np.clip(selectivity * (len(values) - 1), 0, len(values) - 1))
            return values[rank]
        return values[int(self.rng.integers(0, len(values)))]

    def _range_value(self, spec: ParameterSpec, selectivity: float | None) -> Any:
        if spec.range:
            token = spec.range[0]
            if selectivity is not None and len(spec.range) > 1:
                idx = int(np.clip(round(selectivity * (len(spec.range) - 1)), 0, len(spec.range) - 1))
                token = spec.range[idx]
            return apply_range_duration(token)
        days = 30
        if selectivity:
            days = max(1, int(365 * selectivity))
        return datetime.now(UTC) - timedelta(days=days)
