from pathlib import Path

import pytest
from typer.testing import CliRunner

from perf_envelope.cli.app import app
from perf_envelope.config.loader import ProjectLoader
from perf_envelope.config.spec import compile_spec, compile_spec_data
from perf_envelope.exceptions import ConfigError
from perf_envelope.experiment.planner import ExperimentPlanner

runner = CliRunner()


def test_compile_example_target_spec(example_project: Path):
    resolved = compile_spec(example_project / "target.yaml")
    assert resolved.dataset.mode == "existing"
    assert resolved.environment.connection.database == "my_database"
    assert resolved.query.collection == "my_collection"
    assert resolved.dataset.collection == "my_collection"
    assert resolved.slo.latency.p95_ms == 100
    assert resolved.parameters["group_id"].field == "group_id"
    plan = ExperimentPlanner(resolved).plan_dict()
    assert plan["dataset_mode"] == "existing"
    assert plan["coarse_cells"] == 8


def test_spec_matches_existing_experiment_target(example_project: Path):
    from_project = ProjectLoader(example_project).resolve_experiment("existing")
    from_spec = compile_spec(example_project / "target.yaml")
    assert from_spec.environment.connection.database == from_project.environment.connection.database
    assert from_spec.query.collection == from_project.query.collection
    assert from_spec.dataset.mode == from_project.dataset.mode


def test_cli_collection_override_forces_existing(tmp_path: Path):
    spec = tmp_path / "target.yaml"
    spec.write_text(
        """
target:
  database: sales
  collection: orders
query:
  filter: {status: {value: "{{status}}"}}
  parameters:
    status: {type: literal, strategy: fixed, value: OPEN}
data:
  mode: synthetic
  count: 10
  fields:
    status: string
sweep:
  documents: [10, 20]
"""
    )
    resolved = compile_spec(spec, database="other", collection="events")
    assert resolved.dataset.mode == "existing"
    assert resolved.environment.connection.database == "other"
    assert resolved.query.collection == "events"


def test_spec_and_project_together_is_error(example_project: Path):
    result = runner.invoke(
        app,
        [
            "validate",
            "--project",
            str(example_project),
            "--spec",
            str(example_project / "target.yaml"),
        ],
    )
    assert result.exit_code != 0
    assert "either --project or --spec" in result.output


def test_validate_spec(example_project: Path):
    result = runner.invoke(app, ["validate", "--spec", str(example_project / "target.yaml")])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_plan_spec(example_project: Path):
    result = runner.invoke(app, ["plan", "--spec", str(example_project / "target.yaml")])
    assert result.exit_code == 0, result.output
    assert "existing" in result.output


def test_existing_spec_requires_collection():
    with pytest.raises(ConfigError):
        compile_spec_data({"data": {"mode": "existing"}, "query": {"id": "q"}})


def test_synthetic_spec_inline_dataset():
    resolved = compile_spec_data(
        {
            "name": "synth",
            "target": {"database": "perf_test"},
            "query": {"filter": {"id": {"value": "{{id}}"}}, "parameters": {"id": {"field": "id"}}},
            "data": {"mode": "synthetic", "count": 50, "fields": {"id": "integer"}},
            "sweep": {"documents": [50, 100], "concurrency": [1]},
        }
    )
    assert resolved.dataset.mode == "synthetic"
    assert "main" in resolved.dataset.collections
    assert resolved.query.collection == "main"
    plan = ExperimentPlanner(resolved).plan_dict()
    assert plan["coarse_cells"] == 2


def test_external_spec_keeps_document_sweep(example_project: Path):
    resolved = compile_spec(example_project / "external.yaml")
    assert resolved.dataset.mode == "external"
    assert resolved.dataset.collection == "my_collection"
    assert resolved.experiment.targets_existing is False
    assert resolved.experiment.project_documents == [1_000_000]
    plan = ExperimentPlanner(resolved).plan_dict()
    assert plan["dataset_mode"] == "external"
    assert "external generator" in plan["dataset_size_sweep"]
    names = {row["collection"] for row in plan["per_scale_collections"]}
    assert names == {
        "my_collection_10000",
        "my_collection_50000",
        "my_collection_100000",
        "my_collection_500000",
    }
    sizes = sorted({cell["documents"] for cell in plan["cells"]})
    assert sizes == [10000, 50000, 100000, 500000]


def test_external_run_requires_allow_writes(example_project: Path):
    from perf_envelope.exceptions import SafetyError
    from perf_envelope.experiment.runner import ExperimentRunner

    resolved = compile_spec(example_project / "external.yaml")
    runner = ExperimentRunner(resolved, acknowledged=True, allow_external_writes=False)
    with pytest.raises(SafetyError, match="allow-external-writes"):
        runner.run()
