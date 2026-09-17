from perf_envelope.config.models import (
    DEFAULT_COLLECTION,
    DatasetConfig,
    IndexConfig,
    ModelConfig,
    QueryConfig,
    primary_collection,
)
from perf_envelope.config.loader import ProjectLoader


def test_dataset_shorthand_lifts_into_main():
    dataset = DatasetConfig.model_validate(
        {"count": 10, "fields": {"group_id": "integer"}}
    )
    assert DEFAULT_COLLECTION in dataset.collections
    assert dataset.collections[DEFAULT_COLLECTION].count == 10


def test_model_shorthand_lifts_into_main():
    model = ModelConfig.model_validate({"name": "x", "fields": {"group_id": "integer"}})
    assert DEFAULT_COLLECTION in model.collections


def test_index_list_applies_to_main():
    indexes = IndexConfig.model_validate([{"name": "by_group", "keys": {"group_id": 1}}])
    assert DEFAULT_COLLECTION in indexes.indexes
    assert indexes.indexes[DEFAULT_COLLECTION][0].name == "by_group"


def test_index_wrapped_list_applies_to_main():
    indexes = IndexConfig.model_validate({"indexes": [{"name": "by_group", "keys": {"group_id": 1}}]})
    assert list(indexes.indexes) == [DEFAULT_COLLECTION]


def test_query_without_collection_picks_up_primary(example_project):
    loader = ProjectLoader(example_project)
    resolved = loader.resolve_experiment("scale")
    assert resolved.query.collection == "main"
    assert resolved.datasets["referenced"].collections["main"].primary is True


def test_primary_collection_prefers_flag():
    dataset = DatasetConfig.model_validate(
        {
            "collections": {
                "related": {"count": 10, "fields": {"id": "integer"}},
                "main": {"count": 100, "primary": True, "fields": {"id": "integer"}},
            }
        }
    )
    assert primary_collection(dataset) == "main"


def test_primary_collection_existing_uses_named_collection():
    dataset = DatasetConfig(mode="existing", collection="events")
    query = QueryConfig(id="q")
    assert primary_collection(dataset, query_collection=query.collection) == "events"
