"""Build managed collections for access patterns the cluster does not already store."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pymongo import MongoClient
from pymongo.database import Database

from perf_envelope.config.models import CloneEmbedRecipe, IndexSpec, SafetyConfig
from perf_envelope.dataset.guardrails import StorageEstimate, assert_within_limit
from perf_envelope.environment.safety import CreationManifest, assert_managed, is_managed_collection
from perf_envelope.exceptions import ExecutionError
from perf_envelope.models.indexes import ensure_indexes

META_COLLECTION = "perfenv_recipe_meta"


@dataclass
class RecipeMaterialization:
    fingerprint: str
    collections: dict[tuple[str, int], list[str]] = field(default_factory=dict)
    keys: dict[tuple[str, int], list[Any]] = field(default_factory=dict)
    documents: dict[tuple[str, int], int] = field(default_factory=dict)
    reused: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    lookup_field: str = "loan_instance.loan_ruid"

    def as_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "lookup_field": self.lookup_field,
            "reused": list(self.reused),
            "created": list(self.created),
            "collections": {
                f"{database}:{matches}": names
                for (database, matches), names in self.collections.items()
            },
            "documents": {
                f"{database}:{matches}": count
                for (database, matches), count in self.documents.items()
            },
            "notes": list(self.notes),
        }


def collection_names(recipe: CloneEmbedRecipe, matches_per_key: int) -> list[str]:
    return [
        recipe.name_template.format(i=index, matches_per_key=int(matches_per_key))
        for index in range(1, recipe.copies + 1)
    ]


def materialize_recipe(
    client: MongoClient,
    recipe: CloneEmbedRecipe,
    scales: dict[int, str],
    *,
    safety: SafetyConfig,
    manifest: CreationManifest | None = None,
    batch_size: int = 1000,
    override_storage: bool = False,
) -> RecipeMaterialization:
    if not scales:
        raise ExecutionError("A clone_embed recipe needs dataset.scales (document count → database)")
    _assert_storage(recipe, scales, safety, override_storage)
    embed_db, embed_coll = _split_source(recipe.embed.source, next(iter(scales.values())))
    embed_docs = _load_embed(client[embed_db], embed_coll, recipe.embed.fields)
    if not embed_docs:
        raise ExecutionError(f"Embed source {embed_db}.{embed_coll} has no documents")
    lookup_field = _lookup_field(recipe)
    state = RecipeMaterialization(fingerprint="", lookup_field=lookup_field)
    fingerprints: list[str] = []
    for documents, database_name in sorted(scales.items()):
        database = client[database_name]
        source_docs = _load_source(database, recipe)
        if not source_docs:
            raise ExecutionError(
                f"Recipe source {database_name}.{recipe.source_collection} has no documents"
            )
        for matches in recipe.matches_per_key:
            fingerprint = _fingerprint(recipe, int(documents), int(matches), database_name)
            fingerprints.append(fingerprint)
            names = collection_names(recipe, int(matches))
            for name in names:
                assert_managed(name)
            total = (int(documents) // int(matches)) * int(matches)
            if total < int(matches):
                raise ExecutionError(
                    f"documents={documents} is smaller than matches_per_key={matches}"
                )
            key_mode = "synthesized" if (total // int(matches)) > len(embed_docs) else "source"
            if key_mode == "synthesized":
                state.notes.append(
                    f"{database_name} matches_per_key={matches}: lookup keys were made unique "
                    f"because {total // int(matches)} keys were needed and the embed source has "
                    f"{len(embed_docs)} documents. The embedded payload is still a real loan instance."
                )
            if _reuse(database, recipe, fingerprint, names, total):
                state.reused.extend(f"{database_name}.{name}" for name in names)
            else:
                _drop_stale(database, recipe, int(matches), fingerprint)
                _write_copies(
                    database,
                    recipe,
                    names,
                    source_docs,
                    embed_docs,
                    total=total,
                    matches=int(matches),
                    batch_size=batch_size,
                )
                _write_meta(database, recipe, fingerprint, names, total, int(matches), database_name)
                state.created.extend(f"{database_name}.{name}" for name in names)
                if manifest is not None:
                    for name in names:
                        manifest.add(
                            "collection",
                            name,
                            database=database_name,
                            documents=total,
                            recipe=fingerprint,
                        )
            keys = list(database[names[0]].distinct(lookup_field))
            state.collections[(database_name, int(matches))] = names
            state.keys[(database_name, int(matches))] = keys
            state.documents[(database_name, int(matches))] = total
    state.fingerprint = hashlib.sha256(",".join(fingerprints).encode()).hexdigest()[:16]
    return state


def _assert_storage(
    recipe: CloneEmbedRecipe,
    scales: dict[int, str],
    safety: SafetyConfig,
    override: bool,
) -> None:
    documents = 0
    for size in scales:
        for matches in recipe.matches_per_key:
            documents += (int(size) // int(matches)) * int(matches) * recipe.copies
    per_doc = max(1, int(recipe.document_bytes_estimate))
    dataset_bytes = documents * per_doc
    index_bytes = int(dataset_bytes * 0.3 * max(len(recipe.indexes), 1))
    assert_within_limit(
        StorageEstimate(
            documents=documents,
            dataset_bytes=dataset_bytes,
            index_bytes=index_bytes,
            total_bytes=dataset_bytes + index_bytes,
        ),
        safety,
        override=override,
    )


def _load_embed(database: Database, collection: str, fields: list[str]) -> list[dict[str, Any]]:
    projection = {name: 1 for name in fields}
    projection["_id"] = 1
    docs = []
    for doc in database[collection].find({}, projection).batch_size(1000):
        slim = {name: doc.get(name) for name in fields if name in doc or name == "_id"}
        if "_id" in fields:
            slim["_id"] = doc.get("_id")
        docs.append(slim)
    return docs


def _load_source(database: Database, recipe: CloneEmbedRecipe) -> list[dict[str, Any]]:
    projection = {name: 1 for name in recipe.copy_fields}
    projection["_id"] = 0
    return list(database[recipe.source_collection].find({}, projection).batch_size(1000))


def choose_embeds(embed_docs: list[dict[str, Any]], n_keys: int) -> list[dict[str, Any]]:
    """One subdocument per distinct lookup key.

    When the embed source is smaller than the key count, `loan_ruid` is rewritten so
    each key still appears exactly `matches_per_key` times. The other fields stay
    copied from a real loan instance.
    """
    if n_keys <= len(embed_docs):
        return [dict(embed_docs[index]) for index in range(n_keys)]
    chosen: list[dict[str, Any]] = []
    for index in range(n_keys):
        base = dict(embed_docs[index % len(embed_docs)])
        real = base.get("loan_ruid") or base.get("_id") or index
        base["loan_ruid"] = f"{real}#{index}"
        chosen.append(base)
    return chosen


def _write_copies(
    database: Database,
    recipe: CloneEmbedRecipe,
    names: list[str],
    source_docs: list[dict[str, Any]],
    embed_docs: list[dict[str, Any]],
    *,
    total: int,
    matches: int,
    batch_size: int,
) -> None:
    chosen = choose_embeds(embed_docs, total // matches)
    specs = _index_specs(recipe)
    for copy_index, name in enumerate(names):
        print(f"recipe: writing {database.name}.{name} ({total} documents)", flush=True)
        collection = database[name]
        if name in database.list_collection_names():
            collection.drop()
        batch: list[dict[str, Any]] = []
        for offset in range(total):
            source = source_docs[(offset + copy_index) % len(source_docs)]
            doc = {field_name: source.get(field_name) for field_name in recipe.copy_fields}
            if doc.get("created_at") is None:
                doc["created_at"] = datetime.now(UTC) - timedelta(seconds=total - offset)
            doc[recipe.embed.as_field] = chosen[offset // matches]
            batch.append(doc)
            if len(batch) >= batch_size:
                collection.insert_many(batch, ordered=False)
                batch = []
        if batch:
            collection.insert_many(batch, ordered=False)
        ensure_indexes(database, name, specs)


def _index_specs(recipe: CloneEmbedRecipe) -> list[IndexSpec]:
    if recipe.indexes:
        return list(recipe.indexes)
    field_name = recipe.embed.as_field
    key = "loan_ruid" if "loan_ruid" in recipe.embed.fields else "_id"
    return [IndexSpec(keys={f"{field_name}.{key}": 1, "created_at": -1})]


def _lookup_field(recipe: CloneEmbedRecipe) -> str:
    field_name = recipe.embed.as_field
    key = "loan_ruid" if "loan_ruid" in recipe.embed.fields else "_id"
    return f"{field_name}.{key}"


def _reuse(
    database: Database,
    recipe: CloneEmbedRecipe,
    fingerprint: str,
    names: list[str],
    total: int,
) -> bool:
    meta = database[META_COLLECTION].find_one({"_id": fingerprint})
    if not meta:
        return False
    existing = set(database.list_collection_names())
    if any(name not in existing for name in names):
        return False
    counted = int(database[names[0]].count_documents({}))
    tolerance = max(1, int(total * 0.05))
    return abs(counted - total) <= tolerance and meta.get("name_template") == recipe.name_template


def _drop_stale(database: Database, recipe: CloneEmbedRecipe, matches: int, fingerprint: str) -> None:
    meta = database[META_COLLECTION]
    for old in meta.find({"name_template": recipe.name_template, "matches_per_key": matches}):
        if old.get("_id") == fingerprint:
            continue
        for name in old.get("collections") or []:
            if is_managed_collection(str(name)) and name in database.list_collection_names():
                database[name].drop()
        meta.delete_one({"_id": old["_id"]})


def _write_meta(
    database: Database,
    recipe: CloneEmbedRecipe,
    fingerprint: str,
    names: list[str],
    total: int,
    matches: int,
    database_name: str,
) -> None:
    database[META_COLLECTION].replace_one(
        {"_id": fingerprint},
        {
            "_id": fingerprint,
            "kind": recipe.kind,
            "name_template": recipe.name_template,
            "matches_per_key": matches,
            "documents": total,
            "collections": names,
            "database": database_name,
            "source_collection": recipe.source_collection,
            "created_at": datetime.now(UTC).isoformat(),
        },
        upsert=True,
    )


def _fingerprint(recipe: CloneEmbedRecipe, documents: int, matches: int, database: str) -> str:
    payload = {
        "copy_fields": list(recipe.copy_fields),
        "copies": recipe.copies,
        "database": database,
        "documents": documents,
        "embed": recipe.embed.model_dump(by_alias=True),
        "indexes": [spec.model_dump() for spec in _index_specs(recipe)],
        "key_assignment": "unique-loan-ruid-v1",
        "kind": recipe.kind,
        "matches_per_key": matches,
        "seed": recipe.seed,
        "source_collection": recipe.source_collection,
        "template": recipe.name_template,
    }
    encoded = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def _split_source(source: str, default_database: str) -> tuple[str, str]:
    if "." in source:
        database, _, collection = source.partition(".")
        return database, collection
    return default_database, source
