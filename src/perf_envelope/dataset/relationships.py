"""Preserve parent/child identity when generating related collections."""

from __future__ import annotations

from typing import Any

import numpy as np

from perf_envelope.config.models import FieldSpec


def parse_reference(ref: str) -> tuple[str, str]:
    collection, _, field = ref.partition(".")
    if not collection or not field:
        raise ValueError(f"Invalid reference '{ref}', expected collection.field")
    return collection, field


def sample_from_parent(
    parent_values: list[Any],
    n: int,
    spec: FieldSpec,
    rng: np.random.Generator,
) -> list[Any]:
    if not parent_values:
        raise ValueError("Cannot sample references from an empty parent collection")
    values = np.array(parent_values, dtype=object)
    if spec.distribution == "zipf":
        ranks = ((rng.zipf(spec.alpha, size=n) - 1) % len(values))
        return values[ranks].tolist()
    if spec.distribution == "sequential":
        return [parent_values[i % len(parent_values)] for i in range(n)]
    return rng.choice(values, size=n, replace=True).tolist()
