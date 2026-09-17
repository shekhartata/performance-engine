"""Estimate storage before generating datasets."""

from __future__ import annotations

from dataclasses import dataclass

from perf_envelope.config.models import DatasetConfig, SafetyConfig, parse_field_spec
from perf_envelope.exceptions import GuardrailError


def _field_bytes(spec) -> int:
    type_name = spec.type.lower()
    if spec.target_bytes:
        return spec.target_bytes
    if type_name in {"objectid"}:
        return 12
    if type_name in {"integer", "int", "long"}:
        return 8
    if type_name in {"float", "double"}:
        return 8
    if type_name in {"bool", "boolean"}:
        return 1
    if type_name in {"datetime"}:
        return 8
    if type_name == "array":
        size = spec.array_size[1] if isinstance(spec.array_size, tuple) else (spec.array_size or 3)
        item = parse_field_spec(spec.items or {"type": "string"})
        return int(size) * _field_bytes(item)
    if type_name == "object":
        if spec.fields:
            return sum(_field_bytes(parse_field_spec(child)) for child in spec.fields.values())
        return spec.target_bytes or 64
    if spec.cardinality:
        return 8 + max(len(spec.prefix), 3)
    return 24


@dataclass
class StorageEstimate:
    documents: int
    dataset_bytes: int
    index_bytes: int
    total_bytes: int

    @property
    def total_gb(self) -> float:
        return self.total_bytes / (1024**3)

    def as_dict(self) -> dict:
        return {
            "documents": self.documents,
            "dataset_gb": round(self.dataset_bytes / (1024**3), 4),
            "indexes_gb": round(self.index_bytes / (1024**3), 4),
            "total_gb": round(self.total_gb, 4),
        }


def estimate_storage(dataset: DatasetConfig, index_count: int = 1) -> StorageEstimate:
    total_docs = 0
    dataset_bytes = 0
    for spec in dataset.collections.values():
        total_docs += spec.count
        if spec.document_size:
            per_doc = spec.document_size.target_bytes
        else:
            per_doc = 20
            for raw in spec.fields.values():
                per_doc += _field_bytes(parse_field_spec(raw))
        dataset_bytes += spec.count * per_doc
    index_bytes = int(dataset_bytes * 0.15 * max(index_count, 1))
    return StorageEstimate(
        documents=total_docs,
        dataset_bytes=dataset_bytes,
        index_bytes=index_bytes,
        total_bytes=dataset_bytes + index_bytes,
    )


def assert_within_limit(
    estimate: StorageEstimate, safety: SafetyConfig, *, override: bool = False
) -> None:
    if override or safety.allow_override_storage_limit:
        return
    if estimate.total_gb > safety.max_storage_gb:
        raise GuardrailError(
            f"Estimated storage {estimate.total_gb:.2f} GB exceeds configured limit "
            f"{safety.max_storage_gb} GB. Reduce dataset size or pass --override-storage-limit."
        )
