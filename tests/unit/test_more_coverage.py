from datetime import datetime

import numpy as np
import pandas as pd

from perf_envelope.config.models import FieldSpec, ParameterSpec, QueryConfig
from perf_envelope.dataset.distributions import sample_field
from perf_envelope.dataset.generator import generate_all
from perf_envelope.dataset.guardrails import estimate_storage
from perf_envelope.dataset.relationships import parse_reference, sample_from_parent
from perf_envelope.models.schema import logical_collection_names, model_field_map
from perf_envelope.query.canonicalizer import canonical_form
from perf_envelope.query.parameters import QueryParameterGenerator
from perf_envelope.query.parser import apply_range_duration, sort_list, substitute
from perf_envelope.query.selectivity import estimate_selectivity
from perf_envelope.telemetry.latency import summarize_latencies
from perf_envelope.workload.cache import condition_cache, normalize_cache_state


def test_distributions_cover_types():
    rng = np.random.default_rng(0)
    assert all(isinstance(v, int) for v in sample_field(FieldSpec(type="integer", cardinality=5, distribution="random"), 10, rng))
    floats = sample_field(FieldSpec(type="float", min=0, max=1, distribution="uniform"), 10, rng)
    assert all(0 <= v <= 1 for v in floats)
    normals = sample_field(FieldSpec(type="float", min=0, max=10, distribution="normal", mean=5, std=1), 10, rng)
    assert len(normals) == 10
    bools = sample_field(FieldSpec(type="boolean", p=1.0), 8, rng)
    assert all(bools)
    dates = sample_field(FieldSpec(type="datetime", start="30d"), 5, rng)
    assert all(isinstance(v, datetime) for v in dates)
    strings = sample_field(FieldSpec(type="string", cardinality=3, prefix="u"), 5, rng)
    assert all(s.startswith("u") for s in strings)
    fixed = sample_field(FieldSpec(type="integer", distribution="fixed", value=9), 4, rng)
    assert fixed == [9, 9, 9, 9]
    objs = sample_field(FieldSpec(type="object", fields={"n": "integer"}), 2, rng)
    assert "n" in objs[0]
    arrays = sample_field(FieldSpec(type="array", array_size=3, items=FieldSpec(type="integer", cardinality=4)), 2, rng)
    assert len(arrays[0]) == 3
    nested = sample_field(FieldSpec(type="object", target_bytes=8), 1, rng)
    assert "blob" in nested[0]


def test_generate_optional_and_padding():
    from perf_envelope.config.models import DatasetConfig

    dataset = DatasetConfig.model_validate(
        {
            "seed": 1,
            "collections": {
                "items": {
                    "count": 12,
                    "document_size": {"target_bytes": 400},
                    "fields": {
                        "n": {"type": "integer", "cardinality": 12, "distribution": "sequential"},
                        "maybe": {"type": "string", "optional": True, "optional_p": 1.0},
                    },
                }
            },
        }
    )
    docs = generate_all(dataset)
    assert len(docs["items"]) == 12
    assert all("payload" in d or "n" in d for d in docs["items"])
    estimate = estimate_storage(dataset)
    assert estimate.documents == 12
    assert estimate.total_bytes > 0


def test_relationships_helpers():
    coll, field = parse_reference("customers.customer_id")
    assert (coll, field) == ("customers", "customer_id")
    rng = np.random.default_rng(0)
    spec = FieldSpec(type="integer", distribution="sequential")
    values = sample_from_parent([10, 20, 30], 5, spec, rng)
    assert values[0] == 10
    zipf_spec = FieldSpec(type="integer", distribution="zipf", alpha=1.2)
    sampled = sample_from_parent([1, 2, 3], 10, zipf_spec, rng)
    assert set(sampled) <= {1, 2, 3}


def test_parameter_strategies_and_range():
    gen = QueryParameterGenerator(
        {
            "a": ParameterSpec(field="a", strategy="percentile"),
            "b": ParameterSpec(type="datetime_range", strategy="range", range=["7d", "30d"]),
            "c": ParameterSpec(type="literal", strategy="fixed", value=3),
        },
        cached_values={"a": list(range(10))},
    )
    params = gen.next(selectivity=0.9)
    assert params["c"] == 3
    assert params["a"] in range(10)
    assert isinstance(apply_range_duration("7d"), datetime)
    assert apply_range_duration(5) == 5


def test_canonical_aggregate_and_sort_list():
    query = QueryConfig(
        id="agg",
        operation="aggregate",
        collection="orders",
        pipeline=[{"$match": {"x": 1}}, {"$limit": 10}],
    )
    text = canonical_form(query)
    assert "aggregate orders" in text
    assert "$match" in text
    find = QueryConfig(id="f", collection="orders", sort=[("created_at", -1)], filter={"status": "PAID"})
    assert sort_list(find) == [("created_at", -1)]
    nested = substitute({"a": ["{{x}}", 1]}, {"x": 9})
    assert nested == {"a": [9, 1]}


def test_selectivity_without_mongo():
    class Fake:
        def estimated_document_count(self):
            return 100

        def count_documents(self, *_a, **_k):
            return 5

    query = QueryConfig(id="q", collection="orders", filter={"customer_id": {"value": "{{id}}"}})
    assert 0 < estimate_selectivity(Fake(), query, {"id": 1}, total=100) <= 1


def test_schema_helpers(example_project):
    from perf_envelope.config.loader import ProjectLoader

    model = ProjectLoader(example_project).load_model("referenced")
    assert "main" in logical_collection_names(model)
    fields = model_field_map(model)
    assert "group_id" in fields["main"]


def test_index_diff_without_mongo():
    from perf_envelope.config.models import IndexSpec
    from perf_envelope.models.indexes import diff_indexes

    class Fake:
        name = "orders"

        def list_indexes(self):
            return [
                {"name": "_id_", "key": {"_id": 1}},
                {"name": "status_idx", "key": {"status": 1}},
            ]

    specs = [
        IndexSpec(name="customer_orders", keys={"customer_id": 1, "created_at": -1}),
        IndexSpec(name="status_idx", keys={"status": 1}),
    ]
    diff = diff_indexes(Fake(), specs)
    assert diff.missing[0].name == "customer_orders"
    assert "status_idx" in diff.matched


def test_invalid_reference_and_empty_parent():
    import pytest
    from perf_envelope.dataset.relationships import parse_reference, sample_from_parent

    with pytest.raises(ValueError):
        parse_reference("nocolumn")
    with pytest.raises(ValueError):
        sample_from_parent([], 1, FieldSpec(type="integer"), np.random.default_rng(0))
    summary = summarize_latencies([])
    assert summary["p95_ms"] == 0.0
    called = {"n": 0}
    state = condition_cache("hot", lambda: called.__setitem__("n", called["n"] + 1))
    assert state == "estimated_hot"
    assert called["n"] == 1
    condition_cache("cold", lambda: called.__setitem__("n", 99))
    assert called["n"] == 1
    assert normalize_cache_state("estimated_hot") == "estimated_hot"


def test_workload_executor_with_fake_collection():
    from perf_envelope.config.models import QueryConfig, WorkloadConfig
    from perf_envelope.query.parameters import QueryParameterGenerator
    from perf_envelope.workload.concurrency import WorkloadExecutor

    class Cursor(list):
        def max_time_ms(self, _ms):
            return self

    class FakeCollection:
        def find(self, *_a, **_k):
            return Cursor([{"_id": 1}])

    query = QueryConfig(id="q", collection="orders", filter={}, limit=1)
    workload = WorkloadConfig(
        mode="fixed_request_count",
        request_count=6,
        duration_seconds=None,
        concurrency=2,
        warmup_queries=1,
        think_time_ms=0,
        timeout_ms=1000,
    )
    gen = QueryParameterGenerator({})
    executor = WorkloadExecutor(FakeCollection(), query, workload, gen)
    metrics = executor.run(concurrency=2, include_warmup=True, request_count=6)
    assert metrics.successes == 6
    assert metrics.qps >= 0


def test_repository_roundtrip(tmp_path):
    from perf_envelope.storage.runs import ExperimentRepository

    repo = ExperimentRepository(tmp_path)
    run_dir = repo.create("run_abc")
    repo.write_json(run_dir, "hello.json", {"ok": True})
    assert repo.read_json(run_dir, "hello.json")["ok"] is True
    repo.save_observations(run_dir, [{"p95_ms": 1, "nested": {"a": 1}}])
    frame = repo.load_observations(run_dir)
    assert len(frame) == 1
    assert repo.resolve("run_abc").name == "run_abc"
    assert repo.latest().name == "run_abc"
    assert "application_version" in repo.provenance()


def test_existing_dataset_is_read_only():
    from perf_envelope.config.models import DatasetConfig
    from perf_envelope.dataset.generator import generate_into_mongo
    from perf_envelope.exceptions import DatasetError
    import pytest

    dataset = DatasetConfig(mode="existing", collection="orders")
    with pytest.raises(DatasetError):
        generate_into_mongo(None, dataset, {"orders": "orders"})
