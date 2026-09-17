import os

import pytest

from perf_envelope.config.loader import ProjectLoader
from perf_envelope.dataset.generator import generate_into_mongo
from perf_envelope.dataset.verifier import verify_dataset
from perf_envelope.environment.discovery import discover
from perf_envelope.environment.mongodb import connect
from perf_envelope.environment.safety import managed_collection_name
from perf_envelope.models.indexes import ensure_indexes
from perf_envelope.query.parameters import QueryParameterGenerator
from perf_envelope.telemetry.explain import explain_query
from perf_envelope.workload.concurrency import WorkloadExecutor


@pytest.mark.integration
def test_atlas_generate_query_explain(example_project):
    if not os.environ.get("MONGODB_URI"):
        pytest.skip("MONGODB_URI not set; skipping Atlas integration test")
    loader = ProjectLoader(example_project)
    resolved = loader.resolve_experiment("scale")
    session = connect(resolved.environment)
    try:
        meta = discover(session)
        assert meta["mongodb_version"] != "unknown" or meta["topology"]
        dataset = resolved.datasets["referenced"].model_copy()
        small = dataset.model_copy(
            update={
                "collections": {
                    name: spec.model_copy(update={"count": min(spec.count, 80)})
                    for name, spec in dataset.collections.items()
                }
            }
        )
        physical = {
            name: managed_collection_name("it_getting_started_referenced", name)
            for name in small.collections
        }
        generate_into_mongo(session.database, small, physical, batch_size=80)
        report = verify_dataset(session.database, small, physical, tolerance=0.3)
        assert report["ok"]
        idx = resolved.indexes["referenced"]
        for logical, specs in idx.indexes.items():
            if logical in physical:
                diff = ensure_indexes(session.database, physical[logical], specs)
                assert not diff.missing
        collection = session.database[physical["main"]]
        gen = QueryParameterGenerator(resolved.parameters)
        gen.preload(collection, "group_id")
        workload = resolved.workload.model_copy(
            update={"duration_seconds": 2, "concurrency": 2, "warmup_queries": 2, "request_count": 20}
        )
        executor = WorkloadExecutor(collection, resolved.query, workload, gen, selectivity=0.05)
        metrics = executor.run(concurrency=2, include_warmup=True, request_count=20)
        assert metrics.successes > 0
        explain = explain_query(collection, resolved.query, gen.next(0.05))
        assert "docs_examined" in explain or "error" in explain
        for name in physical.values():
            session.database[name].drop()
    finally:
        session.close()
