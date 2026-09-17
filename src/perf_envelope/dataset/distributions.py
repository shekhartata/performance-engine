"""Seeded value generators for synthetic datasets."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from bson import ObjectId

from perf_envelope.config.models import FieldSpec


def _parse_datetime(value: str | None, default: datetime) -> datetime:
    if value is None:
        return default
    if value.endswith("d") and value[:-1].isdigit():
        return datetime.now(UTC) - timedelta(days=int(value[:-1]))
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def sample_ids(spec: FieldSpec, n: int, rng: np.random.Generator) -> np.ndarray:
    cardinality = spec.cardinality or max(n, 1)
    cardinality = max(int(cardinality), 1)
    if spec.distribution == "sequential":
        return (np.arange(n) % cardinality) + 1
    if spec.distribution == "fixed":
        return np.full(n, int(spec.value if spec.value is not None else 1))
    if spec.distribution == "zipf":
        raw = rng.zipf(spec.alpha, size=n)
        return ((raw - 1) % cardinality) + 1
    if spec.distribution == "normal":
        mean = spec.mean if spec.mean is not None else cardinality / 2
        std = spec.std if spec.std is not None else max(cardinality / 6, 1)
        draws = np.clip(np.rint(rng.normal(mean, std, size=n)), 1, cardinality)
        return draws.astype(int)
    return rng.integers(1, cardinality + 1, size=n)


def sample_numeric(spec: FieldSpec, n: int, rng: np.random.Generator) -> np.ndarray:
    lo = spec.min if spec.min is not None else 0.0
    hi = spec.max if spec.max is not None else 1.0
    if spec.distribution == "fixed":
        return np.full(n, float(spec.value if spec.value is not None else lo))
    if spec.distribution == "normal":
        mean = spec.mean if spec.mean is not None else (lo + hi) / 2
        std = spec.std if spec.std is not None else max((hi - lo) / 6, 1e-9)
        return np.clip(rng.normal(mean, std, size=n), lo, hi)
    return rng.uniform(lo, hi, size=n)


def sample_categorical(spec: FieldSpec, n: int, rng: np.random.Generator) -> list[Any]:
    if not spec.values:
        return ["A"] * n
    if isinstance(spec.values, dict):
        keys = list(spec.values.keys())
        weights = np.array([spec.values[k] for k in keys], dtype=float)
        weights = weights / weights.sum()
        return rng.choice(keys, size=n, p=weights).tolist()
    return rng.choice(list(spec.values), size=n).tolist()


def sample_datetime(spec: FieldSpec, n: int, rng: np.random.Generator) -> list[datetime]:
    now = datetime.now(UTC)
    start = _parse_datetime(spec.start, now - timedelta(days=365))
    end = _parse_datetime(spec.end, now)
    start_ts = start.timestamp()
    end_ts = max(end.timestamp(), start_ts + 1)
    draws = rng.uniform(start_ts, end_ts, size=n)
    return [datetime.fromtimestamp(ts, tz=UTC) for ts in draws]


def sample_boolean(spec: FieldSpec, n: int, rng: np.random.Generator) -> list[bool]:
    return (rng.random(n) < spec.p).tolist()


def sample_string(spec: FieldSpec, n: int, rng: np.random.Generator) -> list[str]:
    ids = sample_ids(spec, n, rng)
    return [f"{spec.prefix}{int(i)}" for i in ids]


def sample_field(spec: FieldSpec, n: int, rng: np.random.Generator) -> list[Any]:
    type_name = spec.type.lower()
    if type_name in {"objectid", "object_id"}:
        return [ObjectId() for _ in range(n)]
    if type_name in {"integer", "int", "long"}:
        return [int(v) for v in sample_ids(spec, n, rng)]
    if type_name in {"float", "double", "number"}:
        return [float(v) for v in sample_numeric(spec, n, rng)]
    if type_name in {"bool", "boolean"}:
        return sample_boolean(spec, n, rng)
    if type_name in {"datetime", "timestamp"}:
        return sample_datetime(spec, n, rng)
    if type_name == "categorical" or spec.values:
        return sample_categorical(spec, n, rng)
    if type_name in {"object", "document"}:
        return [_sample_object(spec, rng) for _ in range(n)]
    if type_name == "array":
        return [_sample_array(spec, rng) for _ in range(n)]
    if spec.distribution == "fixed" and spec.value is not None:
        return [spec.value] * n
    return sample_string(spec, n, rng)


def _sample_object(spec: FieldSpec, rng: np.random.Generator) -> dict[str, Any]:
    if not spec.fields:
        size = spec.target_bytes or 64
        return {"blob": rng.bytes(size).hex()}
    from perf_envelope.config.models import parse_field_spec

    out = {}
    for name, raw in spec.fields.items():
        child = parse_field_spec(raw)
        out[name] = sample_field(child, 1, rng)[0]
    return out


def _sample_array(spec: FieldSpec, rng: np.random.Generator) -> list[Any]:
    from perf_envelope.config.models import parse_field_spec

    if isinstance(spec.array_size, tuple):
        size = int(rng.integers(spec.array_size[0], spec.array_size[1] + 1))
    else:
        size = int(spec.array_size or 3)
    item_spec = parse_field_spec(spec.items or FieldSpec(type="string", cardinality=50))
    return sample_field(item_spec, size, rng)
