"""Compile a single-file target spec into a ResolvedExperiment."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from perf_envelope.config.loader import ResolvedExperiment, load_yaml, parse_model, unwrap
from perf_envelope.config.models import (
    DEFAULT_COLLECTION,
    CollectionModel,
    ConnectionConfig,
    DatasetConfig,
    EnvironmentConfig,
    ExecutionSettings,
    ExperimentConfig,
    ExperimentDimensions,
    IndexConfig,
    LatencySLO,
    ModelConfig,
    ParameterSpec,
    ProjectConfig,
    QueryConfig,
    SLOConfig,
    SafetyConfig,
    TargetConfig,
    WorkloadConfig,
    primary_collection,
)
from perf_envelope.exceptions import ConfigError


def compile_spec(
    path: str | Path,
    *,
    database: str | None = None,
    collection: str | None = None,
) -> ResolvedExperiment:
    """Load a single-file spec and compile it to the same object the project loader produces."""
    spec_path = Path(path).resolve()
    return compile_spec_data(
        load_yaml(spec_path),
        source=spec_path,
        database=database,
        collection=collection,
    )


def compile_spec_data(
    raw: dict[str, Any],
    *,
    source: Path | None = None,
    database: str | None = None,
    collection: str | None = None,
) -> ResolvedExperiment:
    if not isinstance(raw, dict):
        raise ConfigError("Spec root must be a mapping")
    data = dict(raw)
    source = source or Path("spec.yaml")
    project_dir = source.parent

    target_raw = dict(data.get("target") or {})
    if database:
        target_raw["database"] = database
    if collection:
        target_raw["collection"] = collection
    target = TargetConfig.model_validate(target_raw) if target_raw else TargetConfig()

    data_raw = dict(data.get("data") or {})
    if collection:
        # CLI --collection matches the project-loader contract: point at existing data.
        data_raw["mode"] = "existing"
        data_raw["collection"] = collection
    if not data_raw.get("mode"):
        data_raw["mode"] = "existing" if target.collection else "synthetic"
    if data_raw.get("mode") == "existing" and target.collection:
        data_raw.setdefault("collection", target.collection)
    dataset = parse_model(DatasetConfig, data_raw, "dataset")

    query_raw = dict(unwrap(data.get("query") or {}, "query"))
    params_raw = {}
    if "parameters" in query_raw:
        params_raw = query_raw.pop("parameters") or {}
    elif data.get("parameters"):
        params_raw = data.get("parameters") or {}
    if "id" not in query_raw:
        query_raw["id"] = "primary"
    query = parse_model(QueryConfig, query_raw)
    parameters = {
        key: ParameterSpec.model_validate(value) for key, value in (params_raw or {}).items()
    }

    model_raw = data.get("model")
    model_name = "default"
    parsed_model: ModelConfig | None = None
    if model_raw is not None:
        model_data = dict(unwrap(model_raw, "model"))
        if "name" not in model_data:
            model_data["name"] = "default"
        parsed_model = parse_model(ModelConfig, model_data)
        model_name = parsed_model.name

    existing = dataset.mode == "existing"
    explicit_collection = dataset.collection or target.collection or query.collection
    if existing:
        if not explicit_collection:
            raise ConfigError("Existing-data spec requires target.collection (or --collection)")
        dataset = dataset.model_copy(update={"collection": explicit_collection})
        query = query.model_copy(update={"collection": explicit_collection})
        experiment_collection = explicit_collection
        logical = explicit_collection
    else:
        logical = query.collection or primary_collection(
            dataset, parsed_model, query_collection=target.collection
        )
        if query.collection is None:
            query = query.model_copy(update={"collection": logical})
        experiment_collection = None
        if dataset.mode == "external" and not dataset.collection:
            dataset = dataset.model_copy(
                update={"collection": query.collection or DEFAULT_COLLECTION}
            )

    model = parsed_model or ModelConfig(name=model_name, collections={logical: CollectionModel()})
    indexes_raw = data.get("indexes")
    indexes = IndexConfig() if indexes_raw is None else parse_model(IndexConfig, indexes_raw)

    sweep_raw = dict(data.get("sweep") or {})
    project_documents = list(sweep_raw.pop("project_documents", []) or [])
    dimensions = _dimensions_from_sweep(sweep_raw)
    slo = _parse_slo(data.get("slo"))
    workload = _parse_workload(data.get("workload"))
    safety = SafetyConfig.model_validate(data.get("safety") or {})
    execution = ExecutionSettings.model_validate(data.get("execution") or {})

    experiment = ExperimentConfig(
        id=str(data.get("id") or data.get("name") or source.stem),
        model=model_name,
        models=[model_name],
        dataset=None if existing else "inline",
        query="primary",
        dimensions=dimensions,
        repetitions=int(data.get("repetitions") or execution.repetitions),
        database=target.database,
        collection=experiment_collection,
        project_documents=project_documents,
    )

    environment = EnvironmentConfig(
        connection=ConnectionConfig(
            uri_env=target.uri_env or "MONGODB_URI",
            uri=target.uri,
            database=target.database or "perf_test",
        ),
        safety=safety,
    )
    project = ProjectConfig(
        name=str(data.get("name") or source.stem),
        safety=safety,
        execution=execution,
        target=target,
        default_slo="inline",
    )
    execution = execution.model_copy(update={"repetitions": experiment.repetitions})
    return ResolvedExperiment(
        project_dir=project_dir,
        project=project,
        environment=environment,
        experiment=experiment,
        dataset=dataset,
        datasets={model_name: dataset},
        query=query,
        queries={model_name: query},
        parameters=parameters,
        parameters_by_model={model_name: parameters},
        workload=workload,
        slo=slo,
        models={model_name: model},
        indexes={model_name: indexes},
        execution=execution,
        extra={"spec": str(source), "target": target.model_dump()},
    )


def _dimensions_from_sweep(raw: dict[str, Any]) -> ExperimentDimensions:
    dims: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, list):
            dims[key] = {"values": value}
        elif isinstance(value, dict) and "values" in value:
            dims[key] = value
        else:
            raise ConfigError(f"sweep.{key} must be a list of values")
    return ExperimentDimensions.model_validate(dims)


def _parse_slo(raw: Any) -> SLOConfig:
    if raw is None:
        return SLOConfig(latency=LatencySLO(p95_ms=100))
    if not isinstance(raw, dict):
        raise ConfigError("slo must be a mapping")
    payload = dict(raw)
    if "latency" not in payload and "p95_ms" in payload:
        payload = {
            "latency": {"p95_ms": payload.get("p95_ms"), "p99_ms": payload.get("p99_ms")},
            "error_rate": payload.get("error_rate") or {},
            "green_fraction": payload.get("green_fraction", 0.7),
        }
    return SLOConfig.model_validate(payload)


def _parse_workload(raw: Any) -> WorkloadConfig:
    if raw is None:
        return WorkloadConfig()
    payload = unwrap(raw, "workload") if isinstance(raw, dict) else raw
    return WorkloadConfig.model_validate(payload)
