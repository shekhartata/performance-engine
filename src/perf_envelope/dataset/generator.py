"""Generate synthetic collections into MongoDB (or in memory)."""

from __future__ import annotations

from typing import Any

import numpy as np
from bson import ObjectId
from pymongo.database import Database

from perf_envelope.config.models import CollectionDatasetSpec, DatasetConfig, FieldSpec, parse_field_spec
from perf_envelope.dataset.distributions import sample_field
from perf_envelope.dataset.relationships import parse_reference, sample_from_parent
from perf_envelope.environment.safety import CreationManifest, assert_managed
from perf_envelope.exceptions import DatasetError


def _pad_document(doc: dict[str, Any], target_bytes: int | None) -> dict[str, Any]:
    if not target_bytes:
        return doc
    encoded = str(doc)
    deficit = target_bytes - len(encoded)
    if deficit > 0:
        doc.setdefault("payload", "x" * deficit)
    return doc


def generate_collection_docs(
    spec: CollectionDatasetSpec,
    n: int,
    rng: np.random.Generator,
    parent_values: dict[str, dict[str, list[Any]]] | None = None,
) -> list[dict[str, Any]]:
    parent_values = parent_values or {}
    columns: dict[str, list[Any]] = {}
    fields = {name: parse_field_spec(raw) for name, raw in spec.fields.items()}
    for name, field_spec in fields.items():
        if field_spec.references:
            parent_coll, parent_field = parse_reference(field_spec.references)
            source = parent_values.get(parent_coll, {}).get(parent_field, [])
            columns[name] = sample_from_parent(source, n, field_spec, rng)
        else:
            columns[name] = sample_field(field_spec, n, rng)
        if field_spec.optional:
            mask = rng.random(n) < field_spec.optional_p
            columns[name] = [None if skip else value for skip, value in zip(mask, columns[name])]
    docs = []
    target = spec.document_size.target_bytes if spec.document_size else None
    for i in range(n):
        doc = {"_id": ObjectId()}
        for name, values in columns.items():
            if values[i] is not None:
                doc[name] = values[i]
        docs.append(_pad_document(doc, target))
    return docs


def generation_order(dataset: DatasetConfig) -> list[str]:
    remaining = list(dataset.collections.keys())
    ordered: list[str] = []
    while remaining:
        progress = False
        for name in list(remaining):
            spec = dataset.collections[name]
            refs = []
            for raw in spec.fields.values():
                field = parse_field_spec(raw)
                if field.references:
                    refs.append(parse_reference(field.references)[0])
            if all(ref in ordered or ref not in dataset.collections for ref in refs):
                ordered.append(name)
                remaining.remove(name)
                progress = True
        if not progress:
            ordered.extend(remaining)
            break
    return ordered


def generate_all(
    dataset: DatasetConfig, rng: np.random.Generator | None = None
) -> dict[str, list[dict[str, Any]]]:
    rng = rng or np.random.default_rng(dataset.seed)
    parent_values: dict[str, dict[str, list[Any]]] = {}
    out: dict[str, list[dict[str, Any]]] = {}
    for name in generation_order(dataset):
        spec = dataset.collections[name]
        docs = generate_collection_docs(spec, spec.count, rng, parent_values)
        out[name] = docs
        extracted: dict[str, list[Any]] = {}
        for field_name in spec.fields:
            extracted[field_name] = [doc.get(field_name) for doc in docs if field_name in doc]
        parent_values[name] = extracted
    return out


def insert_documents(
    database: Database,
    physical_name: str,
    docs: list[dict[str, Any]],
    *,
    batch_size: int = 1000,
    drop_existing: bool = True,
    unmanaged_allowed: bool = False,
) -> int:
    assert_managed(physical_name, unmanaged_allowed=unmanaged_allowed)
    collection = database[physical_name]
    if drop_existing:
        collection.drop()
    if not docs:
        return 0
    inserted = 0
    for start in range(0, len(docs), batch_size):
        batch = docs[start : start + batch_size]
        collection.insert_many(batch, ordered=False)
        inserted += len(batch)
    return inserted


def generate_into_mongo(
    database: Database,
    dataset: DatasetConfig,
    physical_names: dict[str, str],
    *,
    batch_size: int = 1000,
    manifest: CreationManifest | None = None,
    unmanaged_allowed: bool = False,
) -> dict[str, int]:
    if dataset.mode == "existing":
        raise DatasetError("Existing-dataset mode is read-only; refuse to generate or drop collections")
    if dataset.mode == "external":
        raise DatasetError("External-dataset mode is materialized by the configured generator, not the synthetic writer")
    docs_by_coll = generate_all(dataset)
    counts = {}
    for logical, docs in docs_by_coll.items():
        physical = physical_names[logical]
        count = insert_documents(
            database,
            physical,
            docs,
            batch_size=batch_size,
            unmanaged_allowed=unmanaged_allowed,
        )
        counts[physical] = count
        if manifest:
            manifest.add("collection", physical, logical=logical, documents=count)
    return counts
