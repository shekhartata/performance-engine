"""Cartesian experiment-cell generation."""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Any

from perf_envelope.config.models import ExperimentDimensions
from perf_envelope.workload.cache import normalize_cache_state


@dataclass(frozen=True)
class ExperimentCell:
    documents: int
    selectivity: float
    concurrency: int
    cache_state: str
    extras: dict[str, Any] = field(default_factory=dict)

    def key(self) -> tuple:
        extra_items = tuple(sorted((k, str(v)) for k, v in self.extras.items()))
        return (self.documents, self.selectivity, self.concurrency, self.cache_state, extra_items)

    def as_dict(self) -> dict[str, Any]:
        return {
            "documents": self.documents,
            "selectivity": self.selectivity,
            "concurrency": self.concurrency,
            "cache_state": self.cache_state,
            **self.extras,
        }


PRIMARY_DIMS = {"documents", "selectivity", "concurrency", "cache_state"}


def build_matrix(
    dimensions: ExperimentDimensions,
    defaults: dict[str, Any] | None = None,
) -> list[ExperimentCell]:
    defaults = defaults or {}
    values = dimensions.as_dict()
    documents = values.get("documents") or [defaults.get("documents", 1000)]
    selectivity = values.get("selectivity") or [defaults.get("selectivity", 0.01)]
    concurrency = values.get("concurrency") or [defaults.get("concurrency", 1)]
    cache_state = values.get("cache_state") or [defaults.get("cache_state", "hot")]
    extras = {k: v for k, v in values.items() if k not in PRIMARY_DIMS}
    extra_keys = list(extras.keys())
    extra_values = [extras[k] for k in extra_keys] or [[None]]

    cells: list[ExperimentCell] = []
    for n, sel, conc, cache, extra_combo in product(
        documents, selectivity, concurrency, cache_state, product(*extra_values) if extra_keys else [()]
    ):
        extra_map = {}
        if extra_keys:
            extra_map = {k: v for k, v in zip(extra_keys, extra_combo) if v is not None}
        cells.append(
            ExperimentCell(
                documents=int(n),
                selectivity=float(sel),
                concurrency=int(conc),
                cache_state=normalize_cache_state(str(cache)),
                extras=extra_map,
            )
        )
    return cells
