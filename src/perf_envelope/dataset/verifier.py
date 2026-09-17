"""Validate generated distributions and realized selectivity."""

from __future__ import annotations

from typing import Any

from pymongo.collection import Collection
from pymongo.database import Database

from perf_envelope.config.models import DatasetConfig, parse_field_spec


def cardinality(collection: Collection, field: str) -> int:
    return len(collection.distinct(field))


def verify_collection(
    collection: Collection, expected_count: int, fields: dict[str, Any], tolerance: float = 0.15
) -> dict[str, Any]:
    actual = collection.estimated_document_count()
    report: dict[str, Any] = {
        "collection": collection.name,
        "expected_count": expected_count,
        "actual_count": actual,
        "count_ok": abs(actual - expected_count) <= max(1, int(expected_count * tolerance)),
        "fields": {},
    }
    for name, raw in fields.items():
        spec = parse_field_spec(raw)
        info: dict[str, Any] = {}
        if spec.cardinality:
            distinct = cardinality(collection, name)
            info["expected_cardinality"] = spec.cardinality
            info["actual_cardinality"] = distinct
            info["ok"] = distinct <= spec.cardinality * (1 + tolerance) and distinct > 0
        report["fields"][name] = info
    return report


def verify_dataset(
    database: Database,
    dataset: DatasetConfig,
    physical_names: dict[str, str],
    tolerance: float = 0.15,
) -> dict[str, Any]:
    reports = []
    all_ok = True
    for logical, spec in dataset.collections.items():
        physical = physical_names[logical]
        report = verify_collection(database[physical], spec.count, spec.fields, tolerance)
        reports.append(report)
        if not report["count_ok"]:
            all_ok = False
    return {"ok": all_ok, "collections": reports}
