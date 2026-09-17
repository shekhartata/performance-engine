import pytest

from perf_envelope.config.loader import ProjectLoader
from perf_envelope.exceptions import ConfigError
from perf_envelope.experiment.planner import ExperimentPlanner


def test_existing_experiment_points_at_collection(example_project):
    loader = ProjectLoader(example_project)
    resolved = loader.resolve_experiment("existing")
    assert resolved.dataset.mode == "existing"
    assert resolved.dataset.collection == "my_collection"
    assert resolved.environment.connection.database == "my_database"
    assert resolved.query.collection == "my_collection"
    # A single physical collection means a single model.
    assert resolved.model_names == ["referenced"]


def test_cli_overrides_win_over_yaml(example_project):
    loader = ProjectLoader(example_project)
    resolved = loader.resolve_experiment(
        "existing", database="other_db", collection="events"
    )
    assert resolved.environment.connection.database == "other_db"
    assert resolved.dataset.collection == "events"
    assert resolved.query.collection == "events"


def test_overrides_convert_synthetic_experiment_to_existing(example_project):
    loader = ProjectLoader(example_project)
    synthetic = loader.resolve_experiment("scale")
    assert synthetic.dataset.mode == "synthetic"
    assert synthetic.model_names == ["embedded", "referenced"]

    pointed = loader.resolve_experiment("scale", database="prod_db", collection="records")
    assert pointed.dataset.mode == "existing"
    assert pointed.environment.connection.database == "prod_db"
    assert pointed.model_names == ["embedded"]


def test_documents_sweep_disabled_for_existing(example_project):
    loader = ProjectLoader(example_project)
    existing = ExperimentPlanner(loader.resolve_experiment("existing")).plan_dict()
    # 2 selectivity x 2 concurrency x 2 cache = 8, with no documents axis.
    assert existing["coarse_cells"] == 8
    assert existing["dataset_mode"] == "existing"
    assert existing["target_collection"] == "my_collection"
    assert "disabled" in existing["dataset_size_sweep"]

    pointed = ExperimentPlanner(
        loader.resolve_experiment("scale", collection="records")
    ).plan_dict()
    # the scale experiment has 3 document sizes; they collapse away.
    assert pointed["coarse_cells"] == 8


def test_scaling_still_applies_to_synthetic(example_project):
    loader = ProjectLoader(example_project)
    plan = ExperimentPlanner(loader.resolve_experiment("scale")).plan_dict()
    assert plan["dataset_mode"] == "synthetic"
    assert plan["coarse_cells"] == 24
    assert "dataset_size_sweep" not in plan
    sizes = sorted({cell["documents"] for cell in plan["cells"]})
    assert sizes == [500, 1500, 3000]


def test_experiment_requires_dataset_or_collection():
    from perf_envelope.config.models import ExperimentConfig

    with pytest.raises(Exception):
        ExperimentConfig(
            id="x",
            model="m",
            query="q",
            dimensions={"concurrency": {"values": [1]}},
        )
    ok = ExperimentConfig(
        id="x",
        model="m",
        query="q",
        collection="records",
        dimensions={"concurrency": {"values": [1]}},
    )
    assert ok.targets_existing


def test_existing_mode_refuses_generation():
    from perf_envelope.config.models import DatasetConfig
    from perf_envelope.dataset.generator import generate_into_mongo
    from perf_envelope.exceptions import DatasetError

    dataset = DatasetConfig(mode="existing", collection="records")
    with pytest.raises(DatasetError):
        generate_into_mongo(None, dataset, {"records": "records"})
    external = DatasetConfig(
        mode="external",
        generator={"command": "python gen.py --count {documents}"},
    )
    with pytest.raises(DatasetError):
        generate_into_mongo(None, external, {"main": "main"})
