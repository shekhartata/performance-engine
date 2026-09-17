"""Schema helpers for data-model definitions."""

from __future__ import annotations

from perf_envelope.config.models import CollectionModel, FieldSpec, ModelConfig, expand_fields


def collection_field_specs(collection: CollectionModel) -> dict[str, FieldSpec]:
    return expand_fields(collection.fields)


def model_field_map(model: ModelConfig) -> dict[str, dict[str, FieldSpec]]:
    return {name: collection_field_specs(col) for name, col in model.collections.items()}


def logical_collection_names(model: ModelConfig) -> list[str]:
    return list(model.collections.keys())
