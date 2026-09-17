from pathlib import Path

import pytest

from perf_envelope.api.spec_builder import AdvancedOptions, build_spec, parse_scales
from perf_envelope.config.models import DatasetConfig
from perf_envelope.config.spec import compile_spec_data
from perf_envelope.exceptions import ConfigError, ExecutionError
from perf_envelope.experiment.matrix import ExperimentCell
from perf_envelope.experiment.planner import ExperimentPlanner
from perf_envelope.experiment.runner import ExperimentRunner


def test_dataset_scales_requires_existing_mode():
    with pytest.raises(Exception, match="requires mode"):
        DatasetConfig(mode="synthetic", scales={10_000: "db_a"})


def test_dataset_scales_coerce_string_keys():
    dataset = DatasetConfig(
        mode="existing",
        collection="quotes",
        scales={"10000": "mi_transformation", 50_000: "mi_quotes_50000"},
    )
    assert dataset.has_scale_map
    assert dataset.scale_database(10_000) == "mi_transformation"
    assert dataset.scales[50_000] == "mi_quotes_50000"


def test_parse_scales_json_and_dict():
    assert parse_scales('{"10000":"a","50000":"b"}') == {10_000: "a", 50_000: "b"}
    assert parse_scales({10_000: "a"}) == {10_000: "a"}
    assert parse_scales("") is None
    with pytest.raises(ConfigError):
        parse_scales("not-json")


def test_build_spec_existing_scales_sets_documents_and_compiles():
    scales = {
        10_000: "mi_transformation",
        50_000: "mi_quotes_50000",
        100_000: "mi_quotes_100000",
    }
    spec = build_spec(
        uri="mongodb://user:secret@localhost:27017",
        database="mi_transformation",
        collection="quotes",
        query={"_id": "{{quote_ruid}}"},
        advanced=AdvancedOptions(mode="existing", scales=scales, concurrency="1", selectivity="0.01", cache_state="hot"),
    )
    assert spec["data"]["scales"] == scales
    assert spec["sweep"]["documents"] == [10_000, 50_000, 100_000]
    resolved = compile_spec_data(spec, source=Path("ui-run.yaml"))
    assert resolved.dataset.mode == "existing"
    assert resolved.dataset.scales == scales
    plan = ExperimentPlanner(resolved).plan_dict()
    assert plan["dataset_size_sweep"].startswith("enabled")
    assert plan["coarse_cells"] == 3
    assert plan["scale_databases"]["10000"] == "mi_transformation"
    sizes = sorted(cell["documents"] for cell in plan["cells"])
    assert sizes == [10_000, 50_000, 100_000]


def test_build_spec_rejects_documents_missing_from_scales():
    with pytest.raises(ConfigError, match="without scales"):
        build_spec(
            uri="mongodb://localhost",
            database="mi_transformation",
            collection="quotes",
            query={"filter": {"_id": 1}},
            advanced=AdvancedOptions(
                mode="existing",
                scales={10_000: "mi_transformation"},
                documents="10000, 99999",
                concurrency="1",
                selectivity="0.01",
                cache_state="hot",
            ),
        )


def test_planner_rejects_documents_missing_from_scales():
    from dataclasses import replace

    from perf_envelope.config.models import DimensionSpec

    good = build_spec(
        uri="mongodb://localhost",
        database="mi_transformation",
        collection="quotes",
        query={"filter": {"_id": 1}},
        advanced=AdvancedOptions(
            mode="existing",
            scales={10_000: "mi_transformation"},
            documents="10000",
            concurrency="1",
            selectivity="0.01",
            cache_state="hot",
        ),
    )
    resolved = compile_spec_data(good, source=Path("ui-run.yaml"))
    resolved = replace(
        resolved,
        experiment=resolved.experiment.model_copy(
            update={
                "dimensions": resolved.experiment.dimensions.model_copy(
                    update={"documents": DimensionSpec(values=[10_000, 99_999])}
                )
            }
        ),
    )
    with pytest.raises(ConfigError, match="without dataset.scales"):
        ExperimentPlanner(resolved).coarse_cells()


def test_runner_existing_scale_cells_switch_databases():
    scales = {10_000: "db_10k", 50_000: "db_50k"}
    spec = build_spec(
        uri="mongodb://localhost",
        database="db_10k",
        collection="quotes",
        query={"filter": {"_id": 1}},
        advanced=AdvancedOptions(
            mode="existing",
            scales=scales,
            concurrency="1",
            selectivity="0.01",
            cache_state="hot",
        ),
    )
    resolved = compile_spec_data(spec, source=Path("ui-run.yaml"))
    runner = ExperimentRunner(resolved, acknowledged=True, dry_run=True)

    class _Coll:
        def __init__(self, n):
            self.n = n

        def estimated_document_count(self):
            return self.n

    class _DB:
        def __init__(self, name, count):
            self.name = name
            self._count = count

        def list_collection_names(self):
            return ["quotes"]

        def __getitem__(self, _name):
            return _Coll(self._count)

    class _Client:
        def __getitem__(self, name):
            return _DB(name, 10_000 if name == "db_10k" else 50_000)

    class _Session:
        client = _Client()
        database = _DB("db_10k", 10_000)

    cells = [
        ExperimentCell(documents=10_000, selectivity=0.01, concurrency=1, cache_state="hot"),
        ExperimentCell(documents=50_000, selectivity=0.01, concurrency=1, cache_state="hot"),
    ]
    out = runner._existing_cells(_Session(), "default", cells, {"quotes": "quotes"})
    assert [cell.documents for cell in out] == [10_000, 50_000]
    assert out[0].extras["scale_database"] == "db_10k"
    assert out[1].extras["scale_database"] == "db_50k"
    assert runner._database_for_cell(_Session(), "default", out[1]).name == "db_50k"


def test_runner_existing_scale_missing_collection():
    spec = build_spec(
        uri="mongodb://localhost",
        database="db_a",
        collection="quotes",
        query={"filter": {"_id": 1}},
        advanced=AdvancedOptions(
            mode="existing",
            scales={10_000: "db_a"},
            concurrency="1",
            selectivity="0.01",
            cache_state="hot",
        ),
    )
    resolved = compile_spec_data(spec, source=Path("ui-run.yaml"))
    runner = ExperimentRunner(resolved, acknowledged=True, dry_run=True)

    class _DB:
        name = "db_a"

        def list_collection_names(self):
            return ["other"]

    class _Client:
        def __getitem__(self, _name):
            return _DB()

    class _Session:
        client = _Client()
        database = _DB()

    cells = [ExperimentCell(documents=10_000, selectivity=0.01, concurrency=1, cache_state="hot")]
    with pytest.raises(ExecutionError, match="not found in scale database"):
        runner._existing_cells(_Session(), "default", cells, {"quotes": "quotes"})
