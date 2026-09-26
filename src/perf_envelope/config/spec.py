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
    ThresholdGoal,
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
    goal = _parse_goal(data.get("goal"))
    plans_raw = _parse_plans(data.get("plans"))

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
        goal=goal,
    )
    models_map = {model_name: model}
    queries = {model_name: query}
    datasets = {model_name: dataset}
    parameters_by_model = {model_name: parameters}
    indexes_map = {model_name: indexes}
    if plans_raw:
        names = list(plans_raw)
        experiment = experiment.model_copy(update={"models": names, "model": names[0]})
        models_map = {name: model.model_copy(update={"name": name}) for name in names}
        queries = {name: query.model_copy(update={"id": name}) for name in names}
        datasets = {name: dataset for name in names}
        parameters_by_model = {name: parameters for name in names}
        indexes_map = {name: indexes for name in names}

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
        datasets=datasets,
        query=query,
        queries=queries,
        parameters=parameters,
        parameters_by_model=parameters_by_model,
        workload=workload,
        slo=slo,
        models=models_map,
        indexes=indexes_map,
        execution=execution,
        plans=plans_raw,
        extra={"spec": str(source), "target": _redact_target(target), "user_spec": _user_spec(data)},
    )


def _dimensions_from_sweep(raw: dict[str, Any]) -> ExperimentDimensions:
    known = set(ExperimentDimensions.model_fields) - {"extra_axes"}
    dims: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, list):
            spec: Any = {"values": value}
        elif isinstance(value, dict) and "values" in value:
            spec = value
        else:
            raise ConfigError(f"sweep.{key} must be a list of values or {{values, bind}}")
        if key in known:
            dims[key] = spec
        else:
            extra[key] = spec
    if extra:
        dims["extra_axes"] = extra
    return ExperimentDimensions.model_validate(dims)


def _parse_goal(raw: Any) -> ThresholdGoal | None:
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("goal must be a mapping")
    found = raw.get("find_threshold", raw)
    if not isinstance(found, dict) or "axis" not in found:
        raise ConfigError("goal.find_threshold requires an axis")
    return ThresholdGoal.model_validate(found)


def _parse_plans(raw: Any) -> dict[str, dict[str, Any]]:
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("plans must be a mapping of name → plan")
    plans: dict[str, dict[str, Any]] = {}
    for name, plan in raw.items():
        if not isinstance(plan, dict) or "steps" not in plan:
            raise ConfigError(f"plans.{name} must be an object with steps")
        plans[str(name)] = plan
    return plans


def _user_spec(data: dict[str, Any]) -> dict[str, Any]:
    spec = dict(data)
    target = spec.get("target")
    if isinstance(target, dict) and "uri" in target:
        spec["target"] = {key: value for key, value in target.items() if key != "uri"}
    return spec


def _redact_target(target: TargetConfig) -> dict[str, Any]:
    dumped = target.model_dump()
    if dumped.get("uri"):
        dumped["uri"] = "***"
    return dumped


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
