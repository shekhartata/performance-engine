from pathlib import Path

from perf_envelope.config.loader import ProjectLoader
from perf_envelope.config.models import ExperimentConfig, FieldSpec, parse_field_spec
from perf_envelope.query.canonicalizer import canonical_form, shape_id


def test_parse_field_shorthand():
    spec = parse_field_spec("integer")
    assert spec.type == "integer"


def test_unknown_distribution_rejected():
    try:
        FieldSpec(type="integer", distribution="mystery")
        assert False, "should have raised"
    except Exception:
        pass


def test_load_example_project(example_project: Path):
    loader = ProjectLoader(example_project)
    resolved = loader.resolve_experiment("scale")
    assert resolved.project.name == "getting-started"
    assert resolved.model_names == ["embedded", "referenced"]
    assert resolved.slo.latency.p95_ms == 100
    assert resolved.query.operation == "find"
    assert "main" in resolved.datasets["referenced"].collections
    assert resolved.environment.connection.uri_env == "MONGODB_URI"


def test_experiment_requires_model():
    try:
        ExperimentConfig(
            id="x",
            dataset="d",
            query="q",
            dimensions={"documents": {"values": [1]}},
        )
        assert False
    except Exception:
        pass


def test_query_shape_stable(example_project: Path):
    loader = ProjectLoader(example_project)
    resolved = loader.resolve_experiment("scale")
    first = shape_id(resolved.query)
    second = shape_id(resolved.query)
    assert first == second
    text = canonical_form(resolved.query)
    assert "group_id = ?" in text
    assert "100" in text
    assert "{{" not in text
