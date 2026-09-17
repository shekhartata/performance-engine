"""Inspect, diff, and optionally create indexes on managed collections."""

from __future__ import annotations

from dataclasses import dataclass, field

from pymongo.collection import Collection
from pymongo.database import Database

from perf_envelope.config.models import IndexConfig, IndexSpec
from perf_envelope.environment.safety import assert_managed


@dataclass
class IndexDiff:
    collection: str
    missing: list[IndexSpec] = field(default_factory=list)
    extra: list[dict] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)


def _key_tuple(keys: dict[str, int]) -> tuple[tuple[str, int], ...]:
    return tuple((name, int(direction)) for name, direction in keys.items())


def list_indexes(collection: Collection) -> list[dict]:
    return list(collection.list_indexes())


def diff_indexes(collection: Collection, specs: list[IndexSpec]) -> IndexDiff:
    existing = list_indexes(collection)
    existing_by_key = {
        tuple(idx["key"].items()): idx
        for idx in existing
        if idx.get("name") != "_id_"
    }
    diff = IndexDiff(collection=collection.name)
    wanted_keys = set()
    for spec in specs:
        key = _key_tuple(spec.keys)
        wanted_keys.add(key)
        if key in existing_by_key:
            diff.matched.append(spec.name or existing_by_key[key]["name"])
        else:
            diff.missing.append(spec)
    for key, idx in existing_by_key.items():
        if key not in wanted_keys:
            diff.extra.append(idx)
    return diff


def ensure_indexes(
    database: Database,
    physical_name: str,
    specs: list[IndexSpec],
    *,
    unmanaged_allowed: bool = False,
) -> IndexDiff:
    assert_managed(physical_name, unmanaged_allowed=unmanaged_allowed)
    collection = database[physical_name]
    diff = diff_indexes(collection, specs)
    for spec in diff.missing:
        kwargs = {}
        if spec.name:
            kwargs["name"] = spec.name
        if spec.unique:
            kwargs["unique"] = True
        collection.create_index(list(spec.keys.items()), **kwargs)
    return diff_indexes(collection, specs)


def inspect_all(database: Database, mapping: dict[str, list[IndexSpec]]) -> dict[str, IndexDiff]:
    return {
        name: diff_indexes(database[name], specs) if name in database.list_collection_names() else IndexDiff(
            collection=name, missing=list(specs)
        )
        for name, specs in mapping.items()
    }
