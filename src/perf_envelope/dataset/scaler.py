"""Scale collection counts while preserving cardinality ratios."""

from __future__ import annotations

from copy import deepcopy

from perf_envelope.config.models import CollectionDatasetSpec, DatasetConfig, FieldSpec, parse_field_spec


def scale_count(original: int, factor: float) -> int:
    return max(1, int(round(original * factor)))


def scale_field(spec: FieldSpec, factor: float) -> FieldSpec:
    updates: dict = {}
    if spec.cardinality is not None:
        updates["cardinality"] = max(1, int(round(spec.cardinality * factor)))
    if spec.min is not None and spec.max is not None and spec.type in {"integer", "int", "long"}:
        span = spec.max - spec.min
        updates["max"] = spec.min + span * factor
    return spec.model_copy(update=updates)


def scale_dataset(dataset: DatasetConfig, primary: str, target_documents: int) -> DatasetConfig:
    if dataset.mode != "synthetic" or primary not in dataset.collections:
        return dataset
    original = dataset.collections[primary].count
    factor = target_documents / max(original, 1)
    scaled = deepcopy(dataset)
    new_collections: dict[str, CollectionDatasetSpec] = {}
    for name, spec in dataset.collections.items():
        count = target_documents if name == primary else scale_count(spec.count, factor)
        fields = {}
        for field_name, raw in spec.fields.items():
            parsed = parse_field_spec(raw)
            fields[field_name] = scale_field(parsed, factor)
        new_collections[name] = spec.model_copy(update={"count": count, "fields": fields})
    return scaled.model_copy(update={"collections": new_collections})
